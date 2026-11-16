import{request,post}from'./client.js';
export const preview=lot=>request('/purges/preview?lot='+encodeURIComponent(lot));
export const list=()=>request('/purges');
export const detail=id=>request('/purges/'+id);
export const start=lot=>post('/purges',{lot});
export const retry=id=>post('/purges/'+id+'/retry');
