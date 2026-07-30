import os,sys,json,glob,subprocess,hashlib,time,argparse,re,urllib.request,urllib.error,urllib.parse
ROOTS=["/data/library/movies","/data/library/tv"]
TARGETS={"ru":"Russian","uk":"Ukrainian"}
VIDEO_EXT=(".mkv",".mp4",".avi",".m4v",".ts")
L3TO2={"rus":"ru","ukr":"uk","eng":"en","jpn":"ja"}
TEXT_SUB={"subrip","srt","ass","ssa","mov_text","webvtt","text","subviewer"}
CROCELL="192.168.20.10"
WHISPER_URL="http://%s:11436/transcribe"%CROCELL
OLLAMA_URL="http://%s:11435/v1/chat/completions"%CROCELL
GATE_STATUS="http://%s:11435/gatestatus"%CROCELL
TR_MODEL="gemma3-subs"
CACHE_DIR="/opt/suborch/cache"
RADARR=("http://192.168.30.107:7878", os.environ.get("SUBORCH_RADARR_KEY",""))
SONARR=("http://192.168.30.106:8989", os.environ.get("SUBORCH_SONARR_KEY",""))
JELLYFIN=("http://192.168.30.130:8096", os.environ.get("SUBORCH_JELLYFIN_KEY",""))
PLAYLIST_NAME="Subtitle Me"
BATCH=24; SLEEP=120; CTX={}
os.makedirs(CACHE_DIR,exist_ok=True)
class Busy(Exception): pass
def log(m): print("%s  %s"%(time.strftime("%Y-%m-%d %H:%M:%S"),m),flush=True)
def http(url,data=None,headers=None,method=None,timeout=120):
    h=dict(headers or {})
    if isinstance(data,(dict,list)): data=json.dumps(data).encode(); h.setdefault("Content-Type","application/json")
    req=urllib.request.Request(url,data=data,headers=h,method=method)
    with urllib.request.urlopen(req,timeout=timeout) as r: return r.status,r.read()
def norm(l):
    if not l: return None
    l=l.lower(); return L3TO2.get(l,l[:2])
def probe(p):
    try:
        o=subprocess.run(["ffprobe","-v","error","-show_entries","stream=index,codec_type,codec_name:stream_tags=language","-of","json",p],capture_output=True,text=True,timeout=60).stdout
        return json.loads(o).get("streams",[])
    except Exception as e: log("probe fail %s: %s"%(p,e)); return []
def audio_index(streams,lang):
    n=0
    for s in streams:
        if s.get("codec_type")=="audio":
            if norm(s.get("tags",{}).get("language"))==lang: return n
            n+=1
    return None
def sub_index(streams,lang):
    n=0
    for s in streams:
        if s.get("codec_type")=="subtitle":
            if norm(s.get("tags",{}).get("language"))==lang and s.get("codec_name") in TEXT_SUB: return n
            n+=1
    return None
def sub_langs(streams): return {norm(s.get("tags",{}).get("language")) for s in streams if s.get("codec_type")=="subtitle" and s.get("codec_name") in TEXT_SUB}-{None}
def side_labels(video):
    base=os.path.splitext(video)[0]; found={}
    for f in glob.glob(glob.escape(base)+".*.srt"):
        t=os.path.basename(f)[len(os.path.basename(base))+1:-4].split(".")
        if t: found.setdefault(norm(t[0]),set()).add(".".join(t[1:]) or "plain")
    return found
def side_path(video,lang,label): return "%s.%s.%s.srt"%(os.path.splitext(video)[0],lang,label)
def write_srt(path,text):
    open(path,"w",encoding="utf-8",newline="\n").write(text)
    try: os.chmod(path,0o664)
    except Exception: pass
def cache_wav(video,lang): return os.path.join(CACHE_DIR,hashlib.md5(("%s|%s"%(video,lang)).encode()).hexdigest()+".wav")
def whisper_dub(video,streams,lang):
    ai=audio_index(streams,lang); wav=cache_wav(video,lang)
    if not os.path.exists(wav):
        subprocess.run(["ffmpeg","-y","-i",video,"-map","0:a:%d"%ai,"-ac","1","-ar","16000","-c:a","pcm_s16le",wav],capture_output=True,text=True,timeout=1800)
        if not os.path.exists(wav): raise RuntimeError("audio extract failed")
    try:
        st,body=http(WHISPER_URL+"?lang="+lang,data=open(wav,"rb").read(),headers={"Content-Type":"application/octet-stream"},method="POST",timeout=7200)
    except urllib.error.HTTPError as e:
        if e.code==503: raise Busy()
        raise
    except (urllib.error.URLError,TimeoutError,ConnectionError,OSError): raise Busy()
    os.remove(wav); return body.decode("utf-8","replace")
def get_source(video,streams,src):
    base=os.path.splitext(video)[0]
    for c in ["%s.%s.srt"%(base,src),base+".srt"]:
        if os.path.exists(c): return open(c,encoding="utf-8-sig",errors="replace").read()
    si=sub_index(streams,src)
    if si is None: return None
    out=os.path.join(CACHE_DIR,"src.srt")
    if os.path.exists(out): os.remove(out)
    try:
        subprocess.run(["ffmpeg","-y","-i",video,"-map","0:s:%d"%si,"-f","srt",out],capture_output=True,text=True,timeout=600)
    except Exception as e:
        log("source-extract fail %s: %s"%(os.path.basename(video),e)); return None
    if os.path.exists(out): t=open(out,encoding="utf-8-sig",errors="replace").read(); os.remove(out); return t
    return None
def parse_srt(text):
    cues=[]
    for b in re.split(r"\r?\n\r?\n",(text or "").strip()):
        ls=b.splitlines()
        if len(ls)>=2 and "-->" in ls[1]: cues.append([ls[0],ls[1]," ".join(ls[2:])])
        elif len(ls)>=3 and "-->" in ls[2]: cues.append([ls[1],ls[2]," ".join(ls[3:])])
    return cues
def build_srt(cues): return "\n".join("%d\n%s\n%s\n"%(i,ts,tx) for i,(idx,ts,tx) in enumerate(cues,1))+"\n"
def ollama_chat(payload):
    try: st,body=http(OLLAMA_URL,data=payload,timeout=900)
    except urllib.error.HTTPError as e:
        if e.code==503: raise Busy()
        raise
    except (urllib.error.URLError,TimeoutError,ConnectionError,OSError): raise Busy()
    return json.loads(body)["choices"][0]["message"]["content"]
def translate(cues,lang,ctx):
    tgt=TARGETS[lang]
    sysmsg=("You are a professional subtitle translator working through a film/episode in order. "
        "Translate each numbered line into %s. Context - this is from: %s. "
        "Use the preceding lines to keep pronouns, gender, names, terminology and tone consistent across the whole piece. "
        "Do NOT translate the context lines, do NOT merge/split lines, output ONLY the numbered translations, one per input line."%(tgt,ctx))
    res=[None]*len(cues); i=0; hist=[]
    while i<len(cues):
        batch=cues[i:i+BATCH]
        pre=""
        if hist:
            pre="Preceding lines (already translated, for continuity only, do NOT output):\n"+ \
                "\n".join("%s -> %s"%(s,t) for s,t in hist[-4:])+"\n\n"
        user=pre+("Now translate into %s (output only the numbered lines):\n"%tgt)+ \
             "\n".join("%d| %s"%(j+1,c[2]) for j,c in enumerate(batch))
        out=ollama_chat({"model":TR_MODEL,"messages":[{"role":"system","content":sysmsg},{"role":"user","content":user}],"temperature":0.3,"stream":False})
        got={}
        for ln in out.splitlines():
            m=re.match(r"\s*(\d+)\s*[|.)\]]\s*(.*)",ln)
            if m: got[int(m.group(1))]=m.group(2).strip()
        for j in range(len(batch)):
            tr=got.get(j+1,batch[j][2]); res[i+j]=tr; hist.append((batch[j][2],tr))
        i+=BATCH
    return [[c[0],c[1],res[k]] for k,c in enumerate(cues)]
def load_context():
    for base,key in (RADARR,SONARR):
        api="/api/v3/movie" if base==RADARR[0] else "/api/v3/series"
        try:
            st,body=http(base+api,headers={"X-Api-Key":key},timeout=60)
            for it in json.loads(body):
                if it.get("path"): CTX[it["path"]]=("%s. %s"%(it.get("title",""),it.get("overview","") or ""))[:600]
        except Exception as e: log("ctx %s: %s"%(base,e))
def context_for(video):
    best=None
    for p,c in CTX.items():
        if (video==p or video.startswith(p+os.sep)) and (best is None or len(p)>best[0]): best=(len(p),c)
    return best[1] if best else os.path.splitext(os.path.basename(video))[0]
def pick_source(video,streams,lang):
    cand=[l for l in (sub_langs(streams)|set(side_labels(video).keys())) if l not in TARGETS]
    return "en" if "en" in cand else (cand[0] if cand else None)
def has_pending(video,streams):
    lab=side_labels(video)
    for lang in TARGETS:
        have=lab.get(lang,set())
        if "dub" not in have and audio_index(streams,lang) is not None: return True
        if "ai" not in have and pick_source(video,streams,lang): return True
    return False
def process(video,streams,only_best=False):
    lab=side_labels(video); did=False
    for lang in TARGETS:
        have=lab.get(lang,set())
        if "dub" not in have and audio_index(streams,lang) is not None:
            t=time.time(); srt=whisper_dub(video,streams,lang)
            write_srt(side_path(video,lang,"dub"),srt); have.add("dub"); did=True
            log("  DUB %s  %s  (%.0fs)"%(lang,os.path.basename(video),time.time()-t))
        if only_best and "dub" in have: continue
        if "ai" not in have:
            src=pick_source(video,streams,lang)
            if src:
                cues=parse_srt(get_source(video,streams,src))
                if cues:
                    t=time.time(); tr=translate(cues,lang,context_for(video))
                    write_srt(side_path(video,lang,"ai"),build_srt(tr)); did=True
                    log("  AI  %s<-%s  %s  %d lines  (%.0fs)"%(lang,src,os.path.basename(video),len(cues),time.time()-t))
    return did
def gpu_online():
    try:
        st,body=http(GATE_STATUS,timeout=20)
        s=json.loads(body)
        log("gpu: util=%s%% other=%sMB busy=%s"%(s.get("util"),s.get("other_apps_mb"),s.get("busy")))
        return not s.get("busy",True)
    except Exception as e:
        log("gpu status unreachable: %s"%e); return False
def refresh_jellyfin():
    base,key=JELLYFIN
    if "PASTE_" in key: return
    try: http(base+"/Library/Refresh",data=b"",headers={"X-Emby-Token":key},method="POST",timeout=30)
    except Exception as e: log("jellyfin: %s"%e)
def prune_cache(days=7):
    for f in glob.glob(os.path.join(CACHE_DIR,"*.wav")):
        if os.path.getmtime(f)<time.time()-days*86400: os.remove(f)
def jelly(path,method="GET",params=None):
    base,key=JELLYFIN
    url=base+path+(("?"+urllib.parse.urlencode(params)) if params else "")
    st,body=http(url,headers={"X-Emby-Token":key},method=method,timeout=30)
    return json.loads(body) if (body and method=="GET") else None
def playlist_next():
    if "PASTE_" in JELLYFIN[1]: return None
    users=jelly("/Users") or []
    if not users: return None
    uid=users[0]["Id"]
    pls=(jelly("/Users/%s/Items"%uid,params={"IncludeItemTypes":"Playlist","Recursive":"true"}) or {}).get("Items",[])
    pl=next((p for p in pls if p.get("Name")==PLAYLIST_NAME),None)
    if not pl: return None
    items=(jelly("/Playlists/%s/Items"%pl["Id"],params={"userId":uid,"Fields":"Path"}) or {}).get("Items",[])
    for it in items:
        p=it.get("Path")
        if p and os.path.isfile(p) and p.lower().endswith(VIDEO_EXT): vids=[p]
        elif p and os.path.isdir(p): vids=[os.path.join(d,f) for d,_,fs in os.walk(p) for f in fs if f.lower().endswith(VIDEO_EXT)]
        else: vids=[]
        if vids: return pl["Id"],it.get("PlaylistItemId"),vids
        log("[playlist] SKIP no path: %s"%(it.get("Name") or it.get("Id")))
    return None
def playlist_remove(pid,eid):
    if not eid: return
    try: jelly("/Playlists/%s/Items"%pid,method="DELETE",params={"entryIds":eid})
    except Exception as e: log("playlist remove: %s"%e)
def build_plan(vids):
    plan=[]
    for v in vids:
        st=probe(v)
        if has_pending(v,st):
            has=any(t in (sub_langs(st)|set(side_labels(v).keys())) for t in TARGETS)
            plan.append((0 if not has else 1,v))
    plan.sort(key=lambda x:(x[0],x[1])); return plan
def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--force"); ap.add_argument("--limit",type=int,default=0); ap.add_argument("--dry-run",action="store_true")
    a=ap.parse_args(); load_context(); prune_cache()
    vids=[os.path.join(d,f) for r in ROOTS for d,_,fs in os.walk(r) for f in fs if f.lower().endswith(VIDEO_EXT)]
    if a.force:
        matches=[x for x in vids if a.force.lower() in x.lower()]
        for i,v in enumerate(matches):
            if i>0:
                time.sleep(SLEEP)
                if not gpu_online(): log("crocell busy - stopping"); break
            log("FORCE "+v)
            try: process(v,probe(v),True)
            except Busy: log("GPU busy/off - try later"); break
            except Exception as e: log("ERROR %s: %s"%(os.path.basename(v),e))
        refresh_jellyfin(); return
    if a.dry_run:
        for prio,v in build_plan(vids): print(("MISSING " if prio==0 else "extra   ")+v)
        return
    if not gpu_online(): log("crocell off/busy - nothing done"); return
    plan=build_plan(vids); log("backlog: %d titles pending"%len(plan))
    idx=0; first=True; n=0; tried=set()
    while True:
        if not first:
            time.sleep(SLEEP)
            if not gpu_online(): log("crocell busy - stopping cycle"); break
        first=False
        pn=playlist_next()
        if pn and pn[1] not in tried:
            pid,eid,pvids=pn; tried.add(eid)
            try:
                for v in pvids: log("[playlist] "+v); process(v,probe(v),False)
                playlist_remove(pid,eid); n+=1
            except Busy: log("GPU busy - stopping cycle"); break
            except Exception as e: log("ERROR playlist %s: %s"%(eid,e))
            continue
        if idx>=len(plan): log("backlog drained"); break
        prio,v=plan[idx]; idx+=1
        log(("MISSING " if prio==0 else "")+v)
        try:
            if process(v,probe(v),False): n+=1
        except Busy: log("GPU busy - stopping cycle"); break
        except Exception as e: log("ERROR %s: %s"%(os.path.basename(v),e))
        if a.limit and n>=a.limit: break
    refresh_jellyfin(); log("cycle done, %d items updated"%n)
if __name__=="__main__": main()
