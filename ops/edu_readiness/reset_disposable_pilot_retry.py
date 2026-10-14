"""Explicit owner-directed clean A1 retry; isolated test storage only."""
import json,uuid
from pathlib import Path
from recreate_pilot import kube
EVIDENCE=Path(__file__).resolve().parents[2]/'artifacts/edu-workflow-check'
def main():
 receipt={'passed':False,'production_touched':False,'reason':'owner homed A1 and requested retry; discard disposable pilot data rather than manually bypass retained fence'}
 d=json.loads(kube('get','deployment','odin','-o','json'));s=d['spec']['template']['spec']
 assert d['spec']['selector']['matchLabels']=={'app':'odin-edu-pilot'} and s['nodeSelector']['kubernetes.io/hostname']=='kube3'
 c=s['containers'][0];assert c['image']=='docker.io/library/odin-school-candidate:a1-readiness-20261012' and c['imagePullPolicy']=='Never'
 old=next(v['persistentVolumeClaim']['claimName'] for v in s['volumes'] if v['name']=='data');assert old=='odin-a1-clean-20261012'
 pvc=json.loads(kube('get','pvc',old,'-o','json'));assert pvc['metadata']['uid']=='550be793-557f-43c5-ab55-5b999d40a259'
 receipt['discarded_pvc_uid']=pvc['metadata']['uid'];new='odin-a1-retry-'+uuid.uuid4().hex[:8]
 private=Path.home()/'.odin-edu-pilot-private';suffix=uuid.uuid4().hex[:8]
 for name in ('workflow-state.json','education-license-record.json'):
  p=private/name
  if p.exists():p.rename(private/(name+'.before-homed-retry-'+suffix))
 try:
  kube('scale','deployment/odin','--replicas=0');kube('wait','--for=delete','pod','-l','app=odin-edu-pilot','--timeout=60s',timeout=65)
  fresh={'apiVersion':'v1','kind':'PersistentVolumeClaim','metadata':{'name':new,'namespace':'odin-edu-pilot'},'spec':{'accessModes':['ReadWriteOnce'],'storageClassName':'longhorn','resources':{'requests':{'storage':'20Gi'}}}}
  kube('create','-f','-',input=json.dumps(fresh))
  current=json.loads(kube('get','deployment','odin','-o','json'));spec=current['spec']['template']['spec']
  for v in spec['volumes']:
   if v['name']=='data':v['persistentVolumeClaim']['claimName']=new
  current['spec']['replicas']=1;kube('replace','-f','-',input=json.dumps(current))
  kube('delete','pvc',old,'--wait=false');kube('rollout','status','deployment/odin','--timeout=120s',timeout=125)
  receipt.update(passed=True,fresh_pvc=new,image=c['image'])
 finally:(EVIDENCE/'a1-homed-retry-pilot-reset.json').write_text(json.dumps(receipt,indent=2)+'\n')
 print(json.dumps(receipt))
if __name__=='__main__':main()
