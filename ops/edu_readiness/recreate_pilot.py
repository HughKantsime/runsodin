"""Owner-authorized replacement of the disposable Education pilot only."""
import json
from pathlib import Path
import subprocess
import uuid

NAMESPACE='odin-edu-pilot'
BASE='ghcr.io/hughkantsime/odin@sha256:4ca8b96f8e0d41f3a8f046f9d33f678ec91f0a021188488661a368d59cb26e75'
LOCAL='odin-school-candidate:ams-20261012'
SOCKET='/run/k3s/containerd/containerd.sock'
EVIDENCE=Path(__file__).resolve().parents[2]/'artifacts/edu-workflow-check'


def call(*args, input=None, timeout=60):
 r=subprocess.run(args,input=input,capture_output=True,text=True,timeout=timeout)
 if r.returncode:raise RuntimeError('Scoped pilot command failed: '+args[0])
 return r.stdout


def kube(*args,**kwargs):return call('kubectl','-n',NAMESPACE,*args,**kwargs)


def main():
 suffix=uuid.uuid4().hex[:10];helper='candidate-import-'+suffix
 receipt={'passed':False,'namespace':NAMESPACE,'production_touched':False,'helper_node_wide_import_authority':True}
 stage='verify-disposable-target'
 try:
  deployment=json.loads(kube('get','deployment','odin','-o','json'))
  assert deployment['spec']['selector']['matchLabels']=={'app':'odin-edu-pilot'}
  spec=deployment['spec']['template']['spec'];assert spec['nodeSelector']['kubernetes.io/hostname']=='kube3'
  oldclaim=next(v['persistentVolumeClaim']['claimName'] for v in spec['volumes'] if v['name']=='data')
  assert oldclaim=='odin-data'
  pvc=json.loads(kube('get','pvc',oldclaim,'-o','json'));assert pvc['spec']['resources']['requests']['storage']=='20Gi'
  receipt['replaced_pvc_uid']=pvc['metadata']['uid']
  stage='stop-old-pilot';kube('scale','deployment/odin','--replicas=0')
  kube('wait','--for=delete','pod','-l','app=odin-edu-pilot','--timeout=60s',timeout=65)
  stage='create-temporary-administrative-importer'
  pod={'apiVersion':'v1','kind':'Pod','metadata':{'name':helper,'namespace':NAMESPACE,'labels':{'purpose':'owned-candidate-import'}},'spec':{'restartPolicy':'Never','automountServiceAccountToken':False,'nodeSelector':{'kubernetes.io/hostname':'kube3'},'containers':[{'name':'importer','image':BASE,'imagePullPolicy':'Never','command':['python','-c','import time;time.sleep(900)'],'resources':{'requests':{'cpu':'100m','memory':'128Mi'},'limits':{'cpu':'1','memory':'512Mi'}},'volumeMounts':[{'name':'runtime-socket','mountPath':SOCKET},{'name':'k3s-binary','mountPath':'/node/k3s','readOnly':True}]}],'volumes':[{'name':'runtime-socket','hostPath':{'path':SOCKET,'type':'Socket'}},{'name':'k3s-binary','hostPath':{'path':'/usr/local/bin/k3s','type':'File'}}]}}
  kube('create','-f','-',input=json.dumps(pod));kube('wait','--for=condition=Ready','pod/'+helper,'--timeout=60s',timeout=65)
  stage='stream-owned-image'
  with (EVIDENCE/'school-candidate-cluster-import.log').open('w') as log:
   source=subprocess.Popen(['docker','save',LOCAL],stdout=subprocess.PIPE,stderr=log)
   try:
    importer=subprocess.run(['kubectl','-n',NAMESPACE,'exec','-i',helper,'--','/node/k3s','ctr','--address',SOCKET,'--namespace','k8s.io','images','import','--platform','linux/amd64','-'],stdin=source.stdout,stdout=log,stderr=log,timeout=600)
    source.stdout.close();source_code=source.wait(timeout=20)
    assert importer.returncode==0 and source_code==0
   finally:
    if source.poll() is None:source.terminate();source.wait(timeout=10)
  stage='verify-import-reference'
  listing=kube('exec',helper,'--','/node/k3s','ctr','--address',SOCKET,'--namespace','k8s.io','images','list')
  row=next(line.split() for line in listing.splitlines() if line.startswith('docker.io/library/'+LOCAL+' '))
  digest=row[2];assert digest.startswith('sha256:') and 'linux/amd64' in row
  receipt['imported_reference']=row[0];receipt['imported_digest']=digest
  kube('delete','pod',helper,'--wait=true','--timeout=30s')
  assert subprocess.run(['kubectl','-n',NAMESPACE,'get','pod',helper],capture_output=True).returncode!=0
  receipt['importer_removed']=True
  stage='replace-throwaway-storage'
  kube('delete','pvc',oldclaim,'--wait=true','--timeout=60s',timeout=65)
  fresh={'apiVersion':'v1','kind':'PersistentVolumeClaim','metadata':{'name':'odin-data','namespace':NAMESPACE},'spec':{'accessModes':['ReadWriteOnce'],'storageClassName':'longhorn','resources':{'requests':{'storage':'20Gi'}}}}
  kube('create','-f','-',input=json.dumps(fresh))
  current=json.loads(kube('get','deployment','odin','-o','json'));spec=current['spec']['template']['spec'];container=spec['containers'][0]
  container['image']=row[0];container['imagePullPolicy']='Never'
  container['volumeMounts']=[m for m in container['volumeMounts'] if m['name']=='data']
  spec['volumes']=[v for v in spec['volumes'] if v['name']=='data']
  current['spec']['replicas']=1
  kube('replace','-f','-',input=json.dumps(current))
  receipt['fresh_workload_requested']=True;receipt['passed']=True
 except Exception as error:
  receipt['failure_stage']=stage;receipt['failure_type']=type(error).__name__;raise
 finally:
  subprocess.run(['kubectl','-n',NAMESPACE,'delete','pod',helper,'--ignore-not-found','--wait=true','--timeout=30s'],capture_output=True,timeout=35)
  (EVIDENCE/'school-candidate-pilot-recreation.json').write_text(json.dumps(receipt,indent=2)+'\n')
 print(json.dumps(receipt))


if __name__=='__main__':main()
