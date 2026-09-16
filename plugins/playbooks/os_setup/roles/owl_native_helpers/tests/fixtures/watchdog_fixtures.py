"""Run original/candidate watchdog against isolated command and state fixtures."""
from pathlib import Path
import json,os,shlex,subprocess,tempfile,hashlib

src=Path(__file__).resolve().parent
now=2_000_000_000
base=dict(mode='radar',count=0,previous='0',started=now-1000,map='{"timestamp":'+str(now*1000)+',"data":[]}',health=True,calibrate=None,lock_busy=False,docker_missing=False)
cases={
 'healthy':{}, 'stale':{'map':'{"timestamp":'+str((now-100)*1000)+'}'},
 'empty':{'map':''},'old-api-healthy':{'health':False},
 'old-api-stale':{'health':False,'map':'{"timestamp":'+str((now-100)*1000)+'}'},
 'grace':{'started':now-20,'map':''},
 'crash-loop-bypasses-grace':{'count':3,'previous':'2','started':now-20,'map':''},
 'first-count':{'previous':None}, 'spectrum':{'mode':'spectrum','map':''},
 'sdrconnect':{'mode':'sdrconnect','map':''},
 'calibrating':{'calibrate':now-50,'map':''},
 'stale-calibration':{'calibrate':now-1500,'map':''},
 'restart-locked':{'lock_busy':True,'map':''},
 'container-missing':{'docker_missing':True,'map':''},
 'boundary-fresh':{'map':'{"timestamp":'+str((now-60)*1000)+'}'},
 'boundary-stale':{'map':'{"timestamp":'+str((now-61)*1000)+'}'},
}
mock=r'''#!/usr/bin/env python3
import json,os,sys,pathlib
c=json.loads(pathlib.Path(os.environ['CASE_FILE']).read_text());name=pathlib.Path(sys.argv[0]).name;a=sys.argv[1:]
with open(os.environ['EVENT_FILE'],'a') as f:f.write(json.dumps([name,*a])+'\n')
if name=='docker':
 if a[0]=='inspect':
  if c['docker_missing']:sys.exit(1)
  template=a[2]
  if template=='{{.RestartCount}}':print(c['count'])
  elif template=='{{.State.StartedAt}}':print('FIXED-START')
  elif template=='{{.RestartCount}} {{.State.StartedAt}}':print(c['count'],'FIXED-START')
  else:raise ValueError(a)
elif name=='curl':
 if a[-1].endswith('/api/map-health'):
  if not c['health']:sys.exit(22)
  sys.stdout.write(c['map'][:23])
 elif a[-1].endswith('/api/map'):sys.stdout.write(c['map'])
 else:raise ValueError(a)
elif name=='date':print(c['started'] if '-d' in a else 2000000000)
elif name=='stat':print(c['calibrate'])
elif name=='flock':sys.exit(1 if c['lock_busy'] else 0)
elif name=='pgrep':print(999999999)
elif name in ['systemctl','killmock']:pass
else:raise ValueError(name)
'''
results=[]
with tempfile.TemporaryDirectory(prefix='owl-watchdog-unit-') as temp:
 root=Path(temp);commands=root/'commands';commands.mkdir()
 mockfile=commands/'mock';mockfile.write_text(mock);mockfile.chmod(0o755)
 for name in ['docker','curl','date','stat','flock','pgrep','systemctl','killmock']:(commands/name).symlink_to(mockfile)
 for name,overrides in cases.items():
  case=dict(base,**overrides);outputs={}
  for variant in ['original','single','candidate']:
   d=root/(name+'-'+variant);d.mkdir();(d/'case.json').write_text(json.dumps(case));(d/'mode').write_text(case['mode'])
   if case['previous'] is not None:(d/'count').write_text(case['previous']+'\n')
   if case['calibrate'] is not None:(d/'calibrate').touch()
   text=(src/('watchdog-'+variant)).read_text()
   mapping={'/data/retina-gui/mode.txt':d/'mode','/run/blah2-rspduo-watchdog.count':d/'count',
       '/data/retina-gui/restart.lock':d/'restart','/data/retina-gui/calibrate.lock':d/'calibrate'}
   for old,new in mapping.items():text=text.replace(old,str(new))
   # Override the Bash builtin only inside this isolated test copy.
   text=text.replace('#!/bin/bash\n','#!/bin/bash\nkill() { killmock "$@"; }\n',1)
   script=d/'script';script.write_text(text)
   env=dict(os.environ,PATH=str(commands)+os.pathsep+os.environ['PATH'],CASE_FILE=str(d/'case.json'),EVENT_FILE=str(d/'events'))
   result=subprocess.run(['/bin/bash',str(script)],env=env,capture_output=True,text=True,timeout=5)
   events=[json.loads(l) for l in (d/'events').read_text().splitlines()] if (d/'events').exists() else []
   actions=[e for e in events if e[0] in ['systemctl','killmock'] or e[:2]==['docker','compose']]
   outputs[variant]=dict(exit=result.returncode,stdout=result.stdout,actions=actions,count=(d/'count').read_text() if (d/'count').exists() else None,
       inspect_queries=sum(e[:2]==['docker','inspect'] for e in events),map_queries=sum(e[0]=='curl' and e[-1].endswith('/api/map') for e in events),health_queries=sum(e[0]=='curl' and e[-1].endswith('/api/map-health') for e in events))
  a=outputs['original']
  for n in ['single','candidate']:
   b=outputs[n]
   for key in ['exit','stdout','actions','count']:assert a[key]==b[key],(name,n,key,a,b)
  results.append(dict(name=name,pass_=True,original=a,single=outputs['single'],candidate=outputs['candidate']))
receipt=dict(pass_=True,tests=results,sha256={name:hashlib.sha256((src/name).read_bytes()).hexdigest() for name in ['watchdog-original','watchdog-single','watchdog-candidate','test-watchdog.py']},scope='Mocked Docker, curl, clock, service and kill commands; isolated files; no live system changes.')
(src/'UNIT-RESULTS.json').write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps(dict(pass_=True,cases=len(results),healthy=results[0]),indent=2))
