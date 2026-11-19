/* No raw key/session ID is persisted or exposed. HttpOnly cookie belongs to browser. */
(() => {
 'use strict';
 const BASE='/api/recovery/session/';
 async function call(action, key, session) {
  if(location.protocol!=='https:')throw new Error('Для сохранения входа нужен HTTPS.');
  const headers={'Accept':'application/json','X-Recovery-Session':'1'};
  const method=action==='status'?'GET':'POST';
  if(method==='POST')headers['Content-Type']='application/json';
  if(key)headers.Authorization='Bearer '+key;
  if(session)headers['X-Recovery-CSRF']=session.csrf;
  const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),15000);
  try {
   const response=await fetch(BASE+action,{method,headers,body:method==='POST'?'{}':undefined,credentials:'same-origin',cache:'no-store',redirect:'error',referrerPolicy:'no-referrer',signal:controller.signal});
   if(!response.ok)throw new Error('Сеанс не подтверждён. Введите ключ повторно.');
   const raw=await response.text();if(raw.length>4096)throw new Error('Некорректный ответ входа.');
   const data=JSON.parse(raw);
   if(action==='logout')return null;
   if(data.authenticated!==true||!['native','connectivity'].includes(data.mode)||typeof data.csrf!=='string'||!/^[A-Za-z0-9_-]{43}$/.test(data.csrf)||!Number.isInteger(data.expires_in)||data.expires_in<0||data.expires_in>28800)throw new Error('Некорректный ответ входа.');
   return Object.freeze({browserSession:true,csrf:data.csrf,mode:data.mode});
  }finally{clearTimeout(timeout);}
 }
 window.RecoverySession=Object.freeze({login:key=>call('login',key),restore:()=>call('status'),logout:session=>call('logout',null,session)});
})();
