/** @typedef {{id:number,code:string,name:string,temperature:number,dwell:number,pressure:number,note:string}} Process */
/** @typedef {{id:number,code:string|null,process_id:number,lot:string,force:number|null,flagged:boolean,note:string,purged:boolean,purged_at:string|null,purge_id:number|null}} Sample */
/** @typedef {{id:number,title:string,status:'queued'|'running'|'completed'|'failed',snapshot:object,result:object|null,recommendation:string,approved:boolean,purged_lots:string[]|null,purged_at:string|null}} Assessment */
/** @typedef {{id:number,lot:string,status:'purging'|'completed',created_by:string,created_at:string,updated_at:string,completed_at:string|null,error:string,verify_error:string,progress:{samples:number,samples_total:number,assessments:number,assessments_total:number,files:number,files_total:number,failed:Array<{kind:string,ref_id:number,error:string}>}}} PurgeJob */
export const contractsVersion='seal-variance-2';
