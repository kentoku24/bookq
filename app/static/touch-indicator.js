// ── Touch indicator for recordVideo — lazily loaded by app.js only when
// ?touch=1 or localStorage.bookq_touch=1 (see loader in app.js).
// Shows a red circle at touch locations, visible in simctl io recordVideo
// (host overlay is not captured). Unconditional body: the gate lives in the
// loader, so this file runs exactly when the indicator is wanted.
// NOT in sw.js SHELL_ASSETS — offline recording verification has no use.
(function(){
  const dot = document.createElement('div');
  dot.id = 'touch-indicator';
  Object.assign(dot.style, {position:'fixed',width:'44px',height:'44px',borderRadius:'50%',background:'rgba(255,0,0,0.55)',border:'2px solid white',boxShadow:'0 0 8px rgba(0,0,0,0.5)',pointerEvents:'none',zIndex:'99999',display:'none',transform:'translate(-50%,-50%)',willChange:'left,top'});
  const ensureBody=()=>{if(document.body){document.body.appendChild(dot)}else document.addEventListener('DOMContentLoaded',()=>document.body.appendChild(dot),{once:true})};
  ensureBody();
  let hide; const show=(x,y)=>{dot.style.left=x+'px';dot.style.top=y+'px';dot.style.display='block';clearTimeout(hide);hide=setTimeout(()=>dot.style.display='none',600)};
  const pt=e=>e.touches?e.touches[0]:e;
  ['touchstart','touchmove'].forEach(ev=>document.addEventListener(ev,e=>{const p=pt(e);if(p)show(p.clientX,p.clientY)},{passive:true}));
  // Also show on mouse for desktop testing when touch=1
  document.addEventListener('mousedown',e=>show(e.clientX,e.clientY),{passive:true});
  window.showTouch=(x,y,ms=1000)=>{show(x,y);if(ms) clearTimeout(hide),hide=setTimeout(()=>dot.style.display='none',ms)};
  // For idb swipe visualization: animate from (x1,y1) to (x2,y2) over duration
  window.showSwipe=(x1,y1,x2,y2,ms=1000)=>{const s=performance.now();dot.style.display='block';const anim=t=>{const e=Math.min((performance.now()-s)/ms,1);dot.style.left=(x1+(x2-x1)*e)+'px';dot.style.top=(y1+(y2-y1)*e)+'px';if(e<1)requestAnimationFrame(anim);else hide=setTimeout(()=>dot.style.display='none',500)};requestAnimationFrame(anim)};
})();
