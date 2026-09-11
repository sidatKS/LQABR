import base64,hashlib,hmac,json,os,time,urllib.error,urllib.request
URL=os.environ["GTWY_URL"].rstrip("/")
PUB=os.environ.get("PUB_URL",URL).rstrip("/")
ING=os.environ.get("INGRESS_PATH","/hubspot/events")
TICKET=int(os.environ.get("TICKET_ID","330984829675"))
SECRET=os.environ.get("HUBSPOT_APP_SECRET","")
print("target     :",URL)
print("signed-uri :",PUB+ING)
print("ticket id  :",TICKET)
print("app secret :",("present bytes=%d"%len(SECRET)) if SECRET else "NOT SET")
print("-"*70)
fails=[]
def req(m,p,b=None,h=None):
    r=urllib.request.Request(URL+p,data=b,method=m,headers=h or {})
    try:
        with urllib.request.urlopen(r,timeout=60) as x:return x.status,x.read().decode("utf-8","replace")
    except urllib.error.HTTPError as e:return e.code,e.read().decode("utf-8","replace")
    except Exception as e:return 0,"%s: %s"%(type(e).__name__,e)
def check(n,got,want,extra=""):
    ok=got==want
    print("[%s] %s: got %s, want %s"%("PASS" if ok else "FAIL",n,got,want))
    if extra:print("        ",extra[:900])
    if not ok:fails.append(n)
s,b=req("GET","/");check("GET /",s,200,b)
s,b=req("GET","/readyz");print("[INFO] GET /readyz:",s);print("        ",b[:900])
s,b=req("GET","/metrics");check("GET /metrics",s,200,b)
s,b=req("POST",ING,b"[]",{"content-type":"application/json"});check("unsigned POST -> 401",s,401,b)
print("-"*70)
ms=int(time.time()*1000)
ev=[{"eventId":ms,"subscriptionId":0,"portalId":246777241,"occurredAt":ms,"subscriptionType":"ticket.propertyChange","attemptNumber":0,"objectId":TICKET,"propertyName":"blog_summary","propertyValue":"ITEST blog summary %d"%ms,"changeSource":"LQABR_ITEST"}]
body=json.dumps(ev,separators=(",",":"));ts=str(ms)
sig=base64.b64encode(hmac.new(SECRET.encode(),("POST"+PUB+ING+body+ts).encode(),hashlib.sha256).digest()).decode()
s,b=req("POST",ING,body.encode(),{"content-type":"application/json","X-HubSpot-Signature-v3":sig,"X-HubSpot-Request-Timestamp":ts})
print("[SIGNED POST] HTTP",s)
print(b)
if s==401:fails.append("signature rejected 401")
print("-"*70)
s2,b2=req("GET","/metrics");print("[METRICS AFTER]",s2);print(b2[:900])
print("-"*70)
RU=os.environ.get("RESEARCH_URL","").rstrip("/")
RPATH=os.environ.get("RESEARCH_PATH","/research/campaign/a2a")
if RU:
    tok=""
    try:
        mr=urllib.request.Request("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity?audience="+RU,headers={"Metadata-Flavor":"Google"})
        with urllib.request.urlopen(mr,timeout=15) as x:tok=x.read().decode().strip()
    except Exception as e:print("[WARN] id token mint failed:",e)
    print("[DIRECT] research target:",RU+RPATH,"token_len:",len(tok))
    rb=json.dumps({"jsonrpc":"2.0","id":"itest-gtwy","method":"message/send","params":{"metadata":{"objectId":str(TICKET),"subscriptionType":"ticket.propertyChange","limit":"1"}}}).encode()
    rr=urllib.request.Request(RU+RPATH,data=rb,method="POST",headers={"content-type":"application/json","Authorization":"Bearer "+tok})
    try:
        with urllib.request.urlopen(rr,timeout=180) as x:
            print("[DIRECT] HTTP",x.status);print(x.read().decode("utf-8","replace")[:1200])
    except urllib.error.HTTPError as e:
        print("[DIRECT] HTTP",e.code);print(e.read().decode("utf-8","replace")[:1200]);fails.append("direct research HTTP %d"%e.code)
    except Exception as e:
        print("[DIRECT] error:",type(e).__name__,e);fails.append("direct research error")
else:
    print("[SKIP] RESEARCH_URL not set")
print("-"*70);print("RESULT:","OK" if not fails else "FAILED -> "+"; ".join(fails))
raise SystemExit(1 if fails else 0)
