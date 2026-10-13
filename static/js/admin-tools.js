(() => {
  const params=new URLSearchParams(location.search),page=params.get('page') || 'adminPage';
  let last='';
  const timer=setInterval(()=>{
    if(typeof user==='undefined' || !user?.admin || typeof show!=='function' || !document.getElementById('loader')?.classList.contains('hidden') || document.getElementById('content')?.classList.contains('hidden'))return;
    clearInterval(timer);
    document.querySelectorAll('#nav,.studio-header,.fairness-fab,#levelNotice').forEach(n=>n.style.setProperty('display','none','important'));
    const id=params.get('user');
    if(page==='userPage' && /^\d+$/.test(id || '')){selectedUser=Number(id);show('userPage');loadUser();}
    else show(page);
    // Changes made by the established controls keep the parent title in sync.
    new MutationObserver(()=>{const now=document.body.dataset.page;if(now!==last){last=now;parent.postMessage({type:'admin-tool-page',page:now},location.origin);}}).observe(document.body,{attributes:true,attributeFilter:['data-page']});
    if(parent===window){const b=document.createElement('a');b.href='/admin';b.textContent='← Управление';b.style.cssText='display:block;padding:12px;color:#eee';document.body.prepend(b);}
  },100);
  setTimeout(()=>clearInterval(timer),20000);
})();
