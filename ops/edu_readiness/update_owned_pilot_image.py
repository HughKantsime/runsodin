"""Update only the owner-authorized disposable pilot; preserve its fresh PVC."""
import json,subprocess,uuid
from pathlib import Path
from recreate_pilot import kube,SOCKET,NAMESPACE,BASE
EVIDENCE=Path(__file__).resolve().parents[2]/'artifacts/edu-workflow-check'
LOCAL='odin-school-candidate:a1-readiness-20261012'
def main():
 helper='candidate-import-'+uuid.uuid4().hex[:10]
 receipt={'passed':False,'production_touched':False,'storage_preserved':True,'helper_node_wide_import_authority':True}
 try:
  descriptor=json.loads(subprocess.check_output(['docker','image','inspect',LOCAL,'--format','{{json .Descriptor}}'],text=True));assert descriptor['mediaType']=='application/vnd.oci.image.index.v1+json'
  smoke=json.loads((EVIDENCE/'school-candidate-ams-image-smoke.json').read_text());assert smoke['passed'] and smoke['image']==descriptor['digest'] and smoke['architecture']=='amd64'
  d=json.loads(kube('get','deployment','odin','-o','json'));spec=d['spec']['template']['spec']
  assert d['spec']['selector']['matchLabels']=={'app':'odin-edu-pilot'}
  assert spec['nodeSelector']['kubernetes.io/hostname']=='kube3'
  claim=next(v['persistentVolumeClaim']['claimName'] for v in spec['volumes'] if v['name']=='data')
  assert claim=='odin-a1-clean-20261012'
  uid=json.loads(kube('get','pvc',claim,'-o','json'))['metadata']['uid']
  pod={'apiVersion':'v1','kind':'Pod','metadata':{'name':helper,'namespace':NAMESPACE,'labels':{'purpose':'owned-candidate-import'}},'spec':{'restartPolicy':'Never','automountServiceAccountToken':False,'nodeSelector':{'kubernetes.io/hostname':'kube3'},'containers':[{'name':'importer','image':BASE,'imagePullPolicy':'Never','command':['python','-c','import time;time.sleep(600)'],'resources':{'requests':{'cpu':'100m','memory':'128Mi'},'limits':{'cpu':'1','memory':'512Mi'}},'volumeMounts':[{'name':'socket','mountPath':SOCKET},{'name':'binary','mountPath':'/node/k3s','readOnly':True}]}],'volumes':[{'name':'socket','hostPath':{'path':SOCKET,'type':'Socket'}},{'name':'binary','hostPath':{'path':'/usr/local/bin/k3s','type':'File'}}]}}
  kube('create','-f','-',input=json.dumps(pod));kube('wait','--for=condition=Ready','pod/'+helper,'--timeout=60s',timeout=65)
  with (EVIDENCE/'a1-readiness-cluster-import.log').open('w') as log:
   source=subprocess.Popen(['docker','save',LOCAL],stdout=subprocess.PIPE,stderr=log)
   try:
    result=subprocess.run(['kubectl','-n',NAMESPACE,'exec','-i',helper,'--','/node/k3s','ctr','--address',SOCKET,'--namespace','k8s.io','images','import','--platform','linux/amd64','-'],stdin=source.stdout,stdout=log,stderr=log,timeout=300)
    source.stdout.close();assert source.wait(timeout=20)==0 and result.returncode==0
   finally:
    if source.poll() is None:source.terminate();source.wait(timeout=10)
  listing=kube('exec',helper,'--','/node/k3s','ctr','--address',SOCKET,'--namespace','k8s.io','images','list')
  row=next(x.split() for x in listing.splitlines() if x.startswith('docker.io/library/'+LOCAL+' '))
  expected=descriptor['digest'];assert row[2]==expected
  current=json.loads(kube('get','deployment','odin','-o','json'));assert current['metadata']['resourceVersion']==d['metadata']['resourceVersion']
  container=current['spec']['template']['spec']['containers'][0];assert container['imagePullPolicy']=='Never'
  patch=[{'op':'test','path':'/metadata/resourceVersion','value':d['metadata']['resourceVersion']},{'op':'test','path':'/spec/template/spec/containers/0/name','value':container['name']},{'op':'test','path':'/spec/template/spec/containers/0/image','value':container['image']},{'op':'test','path':'/spec/template/spec/containers/0/imagePullPolicy','value':'Never'},{'op':'replace','path':'/spec/template/spec/containers/0/image','value':row[0]}]
  kube('patch','deployment','odin','--type=json','-p',json.dumps(patch))
  kube('rollout','status','deployment/odin','--timeout=120s',timeout=125)
  assert json.loads(kube('get','pvc',claim,'-o','json'))['metadata']['uid']==uid
  receipt.update(passed=True,image=expected,pvc_uid=uid)
 finally:
  result=subprocess.run(['kubectl','-n',NAMESPACE,'delete','pod',helper,'--ignore-not-found','--wait=true','--timeout=60s'],capture_output=True,timeout=65)
  receipt['importer_removed']=result.returncode==0
  (EVIDENCE/'a1-readiness-pilot-update.json').write_text(json.dumps(receipt,indent=2)+'\n')
 print(json.dumps(receipt))
if __name__=='__main__':main()
