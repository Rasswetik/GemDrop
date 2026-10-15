/* Vibration: one helper, one switch in the profile settings. No sounds. */
(function(){
  var KEY='gd_haptics';
  function enabled(){try{return localStorage.getItem(KEY)!=='0'}catch(e){return true}}
  function setEnabled(v){try{localStorage.setItem(KEY,v?'1':'0')}catch(e){}}
  var tg=null;try{tg=window.Telegram&&Telegram.WebApp&&Telegram.WebApp.HapticFeedback||null}catch(e){}
  var PAT={light:[8],select:[6],medium:[16],heavy:[30],success:[14,40,22],warning:[24,50,24],error:[38,40,38,40,60]};
  function raw(kind){
    if(tg){try{
      if(kind==='success'||kind==='warning'||kind==='error')tg.notificationOccurred(kind);
      else if(kind==='select')tg.selectionChanged();
      else tg.impactOccurred(kind);return}catch(e){}}
    try{if(navigator.vibrate)navigator.vibrate(PAT[kind]||PAT.light)}catch(e){}
  }
  var last=0;
  function haptic(kind){if(!enabled())return;var n=Date.now();if(kind==='light'&&n-last<40)return;last=n;raw(kind||'light')}
  window.haptic=haptic;window.hapticEnabled=enabled;window.hapticSet=setEnabled;
  // Telegram calls made by older code respect the switch too
  if(tg){['impactOccurred','notificationOccurred','selectionChanged'].forEach(function(k){var f=tg[k];if(typeof f!=='function')return;tg[k]=function(){if(enabled())try{return f.apply(tg,arguments)}catch(e){}}})}
  // taps on buttons and game cards
  document.addEventListener('pointerdown',function(e){
    var t=e.target&&e.target.closest&&e.target.closest('button:not(:disabled),.game-card,[role=button],.nav a');
    if(t&&!t.classList.contains('is-disabled')&&!t.closest('.cell'))haptic('light');
  },{passive:true,capture:true});
  function boot(){
    var wrap=document.getElementById('pfHaptics');if(!wrap)return;
    var sw=wrap.querySelector('input');sw.checked=enabled();
    sw.onchange=function(){setEnabled(sw.checked);if(sw.checked)raw('success')};
    // wins and losses
    var r=window.reward;if(typeof r==='function'){window.reward=reward=function(){try{haptic('success')}catch(e){}return r.apply(this,arguments)}}
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot);else boot();
})();
