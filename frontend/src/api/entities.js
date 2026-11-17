import {api} from './client.js';
export const datasets={list:()=>api('/datasets'),get:id=>api('/datasets/'+id),create:data=>api('/datasets',{method:'POST',body:JSON.stringify(data)})};
export const jobs={list:()=>api('/jobs'),get:id=>api('/jobs/'+id),create:data=>api('/jobs',{method:'POST',body:JSON.stringify(data)}),approve:id=>api('/jobs/'+id+'/approve',{method:'POST'})};
export const constraints={
  list:datasetId=>api('/datasets/'+datasetId+'/constraint-versions'),
  get:(datasetId,versionId)=>api('/datasets/'+datasetId+'/constraint-versions/'+versionId),
  validate:(datasetId,payload)=>api('/datasets/'+datasetId+'/constraint-versions/validate',{method:'POST',body:JSON.stringify({dataset_id:datasetId,...payload})}),
  create:(datasetId,payload,idempotencyKey)=>api('/datasets/'+datasetId+'/constraint-versions',{method:'POST',body:JSON.stringify({dataset_id:datasetId,...payload}),headers:{'Idempotency-Key':idempotencyKey}}),
  jobs:datasetId=>api('/datasets/'+datasetId+'/jobs')
};
export const reports={list:()=>api('/reports'),create:data=>api('/reports',{method:'POST',body:JSON.stringify(data)}),toggle:id=>api('/reports/'+id+'/toggle',{method:'POST'}),deliveries:()=>api('/deliveries')};
