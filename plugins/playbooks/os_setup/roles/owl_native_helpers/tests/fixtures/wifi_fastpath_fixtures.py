"""Compare original/candidate recovery behavior with isolated command fixtures.

Never contacts NetworkManager or changes host networking. Commands and runtime
paths are replaced in test copies only; production scripts remain unmodified.
"""
from pathlib import Path
import json,os,subprocess,tempfile,sys,hashlib,shlex
p=Path(__file__).resolve().parent
mock=r'''
import json,os,sys
from pathlib import Path
c=json.loads(os.environ['MOCK_CASE']);state=Path(os.environ['MOCK_RUNTIME']);a=sys.argv[1:];name=Path(sys.argv[0]).name
with open(os.environ['MOCK_TRACE'],'a') as f:f.write(json.dumps([name,*a])+'\n')
released=state.exists()
if name=='timeout':os.execvp(a[1],a[1:])
if name=='date':print('100000' if a==['+%s'] else '2026-01-01T00:00:00Z')
elif name=='stat':print('0')
elif name=='sleep':pass
elif name=='iwgetid':print('FixtureWiFi')
elif name=='ip':print('3: wlan0 inet 192.0.2.10/24')
elif name=='iw':
 if c.get('clients',0):print('Station 00:00:00:00:00:01')
elif name=='systemctl':
 if a[0]=='show':print('active' if c.get('portal') and not released else 'inactive')
 elif a[:2]==['stop','wifi-connect']:state.write_text('released')
 else:raise RuntimeError(a)
elif name=='nmcli':
 if a[-2:]==['device','status']:
  if c.get('query_failure'):sys.exit(1)
  fields=a[a.index('-f')+1].split(',')
  ws=c.get('after_release','disconnected') if released else c.get('wifi','connected')
  for dev,typ,s in [('lo','loopback','connected (externally)'),('eth0','ethernet',c.get('ethernet','unavailable')),('wlan0','wifi',ws),('veth0','ethernet','unmanaged')]:
   row=dict(DEVICE=dev,TYPE=typ,STATE=s,CONNECTION=c.get('active_after_release','FixtureWiFi') if released else c.get('active','FixtureWiFi')); print(':'.join(row[k] for k in fields))
 elif 'connection' in a and 'show' in a and '-f' in a:
  fields=a[a.index('-f')+1].split(',')
  if '--active' in a:
   conn=c.get('active','FixtureWiFi')
   if released:conn=c.get('active_after_release','FixtureWiFi')
   if conn:print(':'.join(dict(NAME=conn,TYPE='802-11-wireless',DEVICE='wlan0')[k] for k in fields))
  else:
   for conn in c.get('profiles',['FixtureWiFi','node-setup']):print(':'.join(dict(NAME=conn,TYPE='802-11-wireless')[k] for k in fields))
 elif '-g' in a:
  key=a[a.index('-g')+1]
  print(('yes' if c.get('hidden') else 'no') if key=='802-11-wireless.hidden' else a[-1])
 elif 'wifi' in a and 'list' in a:
  print('\n'.join(c.get('visible',['FixtureWiFi'])))
 elif '--wait' in a and a[-3:-1]==['device','connect']:sys.exit(c.get('connect_rc',0))
 else:raise RuntimeError(a)
else:raise RuntimeError(name)
'''
cases={
 'healthy_wifi':{},
 'healthy_clears_stale_markers':{'markers':{'nudges':'3','last-rc':'1','seen':'99900','reported':'99900'}},
 'ethernet':{'ethernet':'connected'},
 'ethernet_during_outage':{'ethernet':'connected','markers':{'down-since':'99900'}},
 'first_boot':{'started':False},
 'unknown_active':{'active':''},
 'missing_connection_marker':{'active':'--'},
 'spaces_connection':{'active':'Fixture WiFi'},
 'escaped_colon_connection':{'active':r'Fixture\:WiFi'},
 'vertical_bar_connection':{'active':'Fixture|WiFi'},
 'unprovisioned':{'profiles':['node-setup'],'active':'','wifi':'disconnected'},
 'connecting':{'wifi':'connecting (prepare)'},
 'visible_outage':{'wifi':'disconnected'},
 'invisible_outage':{'wifi':'disconnected','visible':[]},
 'hidden_outage':{'wifi':'disconnected','visible':[],'hidden':True},
 'activation_failed':{'wifi':'disconnected','connect_rc':10},
 'portal_with_client':{'portal':True,'active':'node-setup','clients':1},
 'portal_without_home':{'portal':True,'active':'node-setup','visible':[]},
 'portal_release':{'portal':True,'active':'node-setup','after_release':'disconnected'},
 'portal_hidden':{'portal':True,'active':'node-setup','visible':[],'hidden':True},
 'portal_release_connected':{'portal':True,'active':'node-setup','after_release':'connected'},
 'orphan_portal_profile':{'active':'node-setup'},
 'recovered':{'markers':{'down-since':'99800','seen':'99900','nudges':'2','last-rc':'0'}},
 'query_failure':{'query_failure':True,'active':''},
 'long_outage':{'wifi':'disconnected','visible':[],'markers':{'down-since':'90000','reported':'90000','nudges':'3'}},
}
results=[]
with tempfile.TemporaryDirectory(prefix='owl-wifi-test-') as directory:
 base=Path(directory);binpath=base/'bin';binpath.mkdir()
 for command in ['nmcli','systemctl','iw','timeout','date','stat','sleep','iwgetid','ip']:
  file=binpath/command;file.write_text('#!'+sys.executable+'\n'+mock);file.chmod(0o755)
 for name,case in cases.items():
  outputs={}
  for variant in ['original','candidate']:
   work=base/(name+'-'+variant);work.mkdir();run=work/'run';run.mkdir();logs=work/'logs';logs.mkdir()
   if case.get('started',True):(run/'started').touch()
   for k,v in case.get('markers',{}).items():(run/k).write_text(v+'\n')
   source=(p/('wifi-reconnect-'+variant)).read_text()
   source=source.replace('STATE_DIR=/run/wifi-reconnect','STATE_DIR='+shlex.quote(str(run))).replace('LOG_DIR=/data/log','LOG_DIR='+shlex.quote(str(logs))).replace('/usr/sbin/iwgetid','iwgetid')
   script=work/'script';script.write_text(source);trace=work/'trace.jsonl'
   env=dict(os.environ,PATH=str(binpath)+':'+os.environ['PATH'],MOCK_CASE=json.dumps(case),MOCK_RUNTIME=str(work/'released'),MOCK_TRACE=str(trace))
   result=subprocess.run(['bash',str(script)],env=env,capture_output=True,text=True,timeout=15)
   commands=list(map(json.loads,trace.read_text().splitlines()))
   actions=[c for c in commands if (c[0]=='nmcli' and '--wait' in c) or (c[0]=='systemctl' and c[1]=='stop')]
   outputs[variant]=dict(rc=result.returncode,stdout=result.stdout,stderr=result.stderr,actions=actions,files={str(f.relative_to(work)):f.read_text() for root in [run,logs] for f in root.rglob('*') if f.is_file()},nmcli=sum(c[0]=='nmcli' for c in commands))
  a,b=outputs.values();same=all(a[k]==b[k] for k in ['rc','stdout','stderr','actions','files'])
  row=dict(case=name,pass_=same,original_nmcli=a['nmcli'],candidate_nmcli=b['nmcli'])
  if not same:row['outputs']=outputs
  results.append(row)
report={'pass':all(r['pass_'] for r in results),'cases':results,'scripts_sha256':{n:hashlib.sha256((p/('wifi-reconnect-'+n)).read_bytes()).hexdigest() for n in ['original','candidate']}}
(p/'WIFI-UNITS.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2));assert report['pass']
