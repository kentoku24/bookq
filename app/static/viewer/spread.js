/* bookq viewer/spread — spread model, strip management, snapTo, drag & imagePool.
 * Plain global script: defines window.rebuildSpreads, spreadMetrics etc.
 * Depends on window.images, viewW/H, track etc (proxied by app.js at call-time).
 */
(function () {
  var GAP_OUTER = 10;
  var GAP_INNER = 5;
  var CHAPTER_GAP_PX = 20;

  // ── ImagePool ──
  var POOL_SIZE = 6;
  var imagePool = {
    _available: [],
    init: function(){
      for(var i=0;i<POOL_SIZE;i++){
        var img=new Image(); img.decoding="async"; img.draggable=false; img.alt=""; this._available.push(img);
      }
    },
    acquire: function(){
      return this._available.pop() || (function(){
        var img=new Image(); img.decoding="async"; img.draggable=false; img.alt=""; return img;
      })();
    },
    release: function(img){
      img.src=""; img.classList.remove("loaded"); delete img.dataset.deferredSrc; this._available.push(img);
    }
  };
  imagePool.init();
  window.imagePool = imagePool;

  // Shared state getters — app.js owns the truth via window.* getters
  function _getImages(){ return window.images || []; }
  function _getViewW(){ return window.viewW != null ? window.viewW : window.innerWidth; }
  function _getViewH(){ return window.viewH != null ? window.viewH : window.innerHeight; }
  function _getPageAR(){ return window.pageAR || {}; }
  function _getLastAR(){ return window.lastAR != null ? window.lastAR : 0.7; }

  // Spread model lives on window
  function invalidateSpreadSizes(){ window.spreadSizeCache = {}; }
  window.invalidateSpreadSizes = invalidateSpreadSizes;

  function rebuildSpreads(){
    var images = _getImages();
    var pageMode = window.pageMode || 'single';
    var spreadOffset = window.spreadOffset || 0;
    var n = images.length;
    var spreads = [];
    var idxToSpread = new Array(n);
    function isSpacer(i){ return images[i] && images[i].isSpacer; }
    var groups=[];
    if(pageMode==='spread'){
      var i=0;
      while(i<spreadOffset && i<n){ groups.push([i]); i++; }
      while(i<n){
        var g=(i+1<n)?[i,i+1]:[i];
        groups.push(g);
        i+=g.length;
      }
    } else {
      for(var ii=0;ii<n;ii++) groups.push([ii]);
    }
    for(var g of groups){
      var pages=g.filter(function(i){ return !isSpacer(i); });
      if(!pages.length) continue;
      var s=spreads.length;
      for(var p of pages) idxToSpread[p]=s;
      var chapterBoundaries = window.chapterBoundaries || [];
      spreads.push({pages, chapterStart: chapterBoundaries.indexOf(pages[0])>0});
    }
    window.spreads = spreads;
    window.idxToSpread = idxToSpread;
    if(window.anchorSpread >= spreads.length) window.anchorSpread=Math.max(0, spreads.length-1);
    invalidateSpreadSizes();
  }
  function spreadOf(idx){
    var idxToSpread = window.idxToSpread || [];
    var s=idxToSpread[idx];
    return s===undefined?0:s;
  }
  function arOf(idx){ var pa=window.pageAR||{}; return pa[idx] || (window.lastAR||0.7); }
  function spreadMetrics(s){
    var cache=window.spreadSizeCache||{};
    var cached=cache[s];
    if(cached) return cached;
    var spreads=window.spreads||[];
    var spread=spreads[s];
    if(!spread) return {width:0,height:0,pageWidths:[]};
    var viewW=_getViewW(), viewH=_getViewH();
    var ars=spread.pages.map(arOf);
    var arSum=ars.reduce(function(a,b){return a+b;},0)||1;
    var gaps=(spread.pages.length-1)*GAP_INNER;
    var h=viewH; var w=arSum*h+gaps;
    if(w>viewW){ h=(viewW-gaps)/arSum; w=viewW; }
    var m={width:w,height:h,pageWidths: ars.map(function(a){return a*h;})};
    cache[s]=m; window.spreadSizeCache=cache;
    return m;
  }
  function gapBefore(s){
    var spreads=window.spreads||[];
    var viewW=_getViewW();
    var base;
    if(spreads[s] && spreads[s].chapterStart){
      base=CHAPTER_GAP_PX;
    } else {
      var leftover=(viewW-spreadMetrics(window.anchorSpread||0).width)/2;
      base=leftover<=0?GAP_OUTER:Math.min(GAP_OUTER, leftover/2);
    }
    // Widen the gap so a narrower neighbour (a solo cover, a solo trailing
    // page, a chapter-start spread, or any spread that doesn't fill the
    // viewport) never lets the spread on the OTHER side of it peek into
    // view: if either s-1 or s is centred with leftover margin, the gap
    // must be at least that leftover, so the far spread starts at (or
    // past) the viewport edge instead of bleeding into the centred
    // spread's margin. This applies at a chapter seam too — CHAPTER_GAP_PX
    // is only the floor there, same as GAP_OUTER elsewhere.
    var prevLeftover=(viewW-spreadMetrics(s-1).width)/2;
    var curLeftover=(viewW-spreadMetrics(s).width)/2;
    return Math.max(base, prevLeftover, curLeftover);
  }
  function spreadLeft(s){
    var x=0; var anchor=window.anchorSpread||0;
    if(s>anchor){
      for(var k=anchor+1;k<=s;k++) x-=spreadMetrics(k).width+gapBefore(k);
    } else if(s<anchor){
      for(var kk=s+1;kk<=anchor;kk++) x+=spreadMetrics(kk).width+gapBefore(kk);
    }
    return x;
  }
  function pageOffsetPx(idx){
    var s=spreadOf(idx);
    return (_getViewW()-spreadMetrics(s).width)/2 - spreadLeft(s);
  }

  // EFFECTIVE PATH (Task 4 analysis): index.html loads spread.js BEFORE app.js
  // (both defer, so document order wins). app.js never overwrites window.sliderMax/
  // window.snapTo — its sliderMax/idxToSlider/sliderToIdx/snapTo are IIFE-locals and
  // every live call site (slider input, open-folder, keys, drag, hash restore) uses
  // those locals. So the app.js locals WIN at runtime; the functions below are the
  // dormant twin (reached only via window.snapTo/goNext/goPrev/updateOrientation,
  // whose resize listener is disabled). Formulas are kept character-identical to
  // app.js:648-653 on purpose — T3 (chapter-local swap) replaces the app.js locals
  // AND syncSliderValue below in one step; do not invent new semantics here.
  // slider helpers: one slider step == one spread
  function sliderMax(){ return Math.max(0, (window.spreads||[]).length-1); }
  function idxToSlider(globalIdx){ return spreadOf(globalIdx); }
  function sliderToIdx(val){
    var spreads=window.spreads||[];
    var s=spreads[Math.max(0, Math.min(val, spreads.length-1))];
    return s? s.pages[0]:0;
  }
  window.sliderMax = sliderMax; window.idxToSlider=idxToSlider; window.sliderToIdx=sliderToIdx;
  // Single slider-write point inside this file: snapTo and updateOrientation both
  // route pageSlider.value through here so the two can never disagree.
  function syncSliderValue(idx){
    var pageSlider=document.getElementById("page-slider");
    if(pageSlider) pageSlider.value=idxToSlider(idx);
  }

  // strip elements — window.strip* are owned by app.js but we manipulate them
  function ensureSpreadEl(s){
    var spreadEls=window.spreadEls||(window.spreadEls={});
    if(spreadEls[s]) return spreadEls[s];
    var el=document.createElement("div");
    el.className="spread-container";
    el.style.gap=GAP_INNER+"px";
    spreadEls[s]=el;
    var track=document.getElementById("img-track");
    if(track) track.appendChild(el);
    window.spreadEls=spreadEls;
    return el;
  }
  function placeCell(cell, idx){
    var s=spreadOf(idx);
    var pos=(window.spreads && window.spreads[s]) ? window.spreads[s].pages.indexOf(idx):0;
    cell.dataset.pos=String(pos);
    var el=ensureSpreadEl(s);
    var ref=null;
    for(var child of el.children){
      if(Number(child.dataset.pos)>pos){ ref=child; break; }
    }
    el.insertBefore(cell, ref);
  }
  function regroupCells(){
    var spreadEls=window.spreadEls||{};
    for(var k of Object.keys(spreadEls)){ spreadEls[k].remove(); delete spreadEls[k]; }
    window.spreadEls=spreadEls;
    var stripCells=window.stripCells||{};
    for(var kk of Object.keys(stripCells)) placeCell(stripCells[kk], Number(kk));
  }
  function recordAR(idx, img){
    if(!img.naturalWidth || !img.naturalHeight) return false;
    var ar=img.naturalWidth/img.naturalHeight;
    window.lastAR=ar;
    var pa=window.pageAR||{}; if(pa[idx]===ar) return false;
    pa[idx]=ar; window.pageAR=pa;
    var cache=window.spreadSizeCache||{}; delete cache[spreadOf(idx)]; window.spreadSizeCache=cache;
    return true;
  }
  function getUrl(idx){
    if(window.getUrl) return window.getUrl(idx);
    var images=_getImages(); var img=images[idx];
    if(!img || img.isSpacer) return "";
    return window.OfflineCore.imageUrl(img.folder, img.name);
  }
  function ensureStripImage(idx){
    var images=_getImages();
    if(idx<0||idx>=images.length) return null;
    var imgData=images[idx];
    if(imgData && imgData.isSpacer) return null;
    var stripImgs=window.stripImgs||(window.stripImgs={});
    var stripCells=window.stripCells||(window.stripCells={});
    if(stripImgs[idx]) return stripImgs[idx];
    var cell=document.createElement("div"); cell.className="page-cell";
    var img=imagePool.acquire();
    img.style.setProperty("--i", String(idx));
    img.addEventListener("load", function(){
      if(recordAR(idx, img)){
        if(window.updateStripPositions) window.updateStripPositions();
        if(!window.sliding && spreadOf(idx)===(window.anchorSpread||0)){
          if(window.applyTrackTransform) window.applyTrackTransform(window.currentIdx||0,0,false);
        }
      }
      img.classList.add("loaded");
    }, {once:true});
    img.addEventListener("error", function(){ img.classList.add("loaded"); }, {once:true});
    img.src=getUrl(idx);
    cell.appendChild(img);
    stripImgs[idx]=img; stripCells[idx]=cell;
    window.stripImgs=stripImgs; window.stripCells=stripCells;
    placeCell(cell, idx);
    return img;
  }
  function ensureWindow(idx){
    var images=_getImages(); if(!images.length) return;
    var PREFETCH_AHEAD=window.PREFETCH_AHEAD||8;
    var PREFETCH_BEHIND=window.PREFETCH_BEHIND||3;
    var lo=Math.max(0, idx-PREFETCH_BEHIND);
    var hi=Math.min(images.length-1, idx+PREFETCH_AHEAD);
    for(var i=lo;i<=hi;i++) ensureStripImage(i);
  }
  function evictStrip(idx){
    var stripImgs=window.stripImgs||{};
    var stripCells=window.stripCells||{};
    var spreadEls=window.spreadEls||{};
    var EVICT_AHEAD=window.EVICT_AHEAD||14, EVICT_BEHIND=window.EVICT_BEHIND||6;
    for(var k of Object.keys(stripImgs)){
      var i=Number(k);
      if(i<idx-EVICT_BEHIND || i>idx+EVICT_AHEAD){
        var img=stripImgs[k]; var cell=stripCells[k];
        if(cell) cell.remove();
        if(img) imagePool.release(img);
        delete stripImgs[k]; delete stripCells[k];
      }
    }
    window.stripImgs=stripImgs; window.stripCells=stripCells;
    for(var kk of Object.keys(spreadEls)){
      if(!spreadEls[kk].children.length){ spreadEls[kk].remove(); delete spreadEls[kk]; }
    }
    window.spreadEls=spreadEls;
  }
  function isTrackSlideEnd(e){ return !e || (e.target===document.getElementById("img-track") && e.propertyName==="transform"); }
  function applyTrackTransform(idx, dragPx, animate){
    var track=document.getElementById("img-track");
    if(!track) return;
    if(animate){
      track.style.transition="transform .3s cubic-bezier(0.25,0.1,0.25,1.0)";
      track.offsetHeight;
    } else track.style.transition="none";
    var off=pageOffsetPx(idx);
    track.style.transform="translateX("+(off+(dragPx||0))+"px)";
  }
  function updateStripPositions(){
    var spreadEls=window.spreadEls||{};
    for(var k in spreadEls){
      var s=Number(k); var el=spreadEls[s];
      var spreads=window.spreads||[]; var spread=spreads[s];
      if(!spread) continue;
      var m=spreadMetrics(s);
      el.style.width=m.width+"px"; el.style.height=m.height+"px"; el.style.left=spreadLeft(s)+"px";
      spread.pages.forEach(function(idx,i){
        var stripCells=window.stripCells||{};
        var cell=stripCells[idx];
        if(cell){ cell.style.width=m.pageWidths[i]+"px"; cell.style.height=m.height+"px"; }
      });
    }
    // chapter labels
    var chapterLabels=window.chapterLabels||[];
    var chapterBoundaries=window.chapterBoundaries||[];
    for(var j=0;j<chapterLabels.length;j++){
      var ss=spreadOf(chapterBoundaries[j+1]);
      var mm=spreadMetrics(ss);
      chapterLabels[j].style.left=(spreadLeft(ss)+mm.width+gapBefore(ss)/2)+"px";
    }
  }
  function rebaseAnchor(idx){
    window.anchorSpread=spreadOf(idx);
    updateStripPositions();
    applyTrackTransform(idx,0,false);
  }
  function snapTo(idx, animate){
    if(animate===undefined) animate=true;
    var images=_getImages();
    if(idx<0||idx>=images.length) return;
    window.currentIdx=idx;
    syncSliderValue(idx);
    // counter
    var counter=document.getElementById("counter");
    var folderNameEl=document.getElementById("folder-name");
    var chapterList=window.chapterList||[];
    var currentFolder=window.currentFolder;
    if(chapterList.length>1){
      var realIdx=(window.globalToRealIdx && window.globalToRealIdx[idx]!==undefined)? window.globalToRealIdx[idx]:-1;
      if(realIdx>=0){
        var info=window.getCurrentChapterInfo ? window.getCurrentChapterInfo(idx) : {chapterName:"", chapterIdx:0, localIdx: idx};
        if(counter) counter.textContent=(realIdx+1)+" / "+(window.realImageCount||images.length);
        if(folderNameEl) folderNameEl.textContent=info.chapterName;
      } else {
        if(counter) counter.textContent=(idx+1)+" / "+images.length;
      }
    } else {
      if(counter) counter.textContent=(idx+1)+" / "+images.length;
      if(folderNameEl) folderNameEl.textContent=currentFolder?currentFolder.split("/").pop():"";
    }
    if(!window.urlSyncPaused){
      if(currentFolder && chapterList.length>1){
        var info2=window.getCurrentChapterInfo ? window.getCurrentChapterInfo(idx) : null;
        var chapter=info2? chapterList[info2.chapterIdx]:null;
        if(chapter && window.ReadingProgress) window.ReadingProgress.saveDebounced(chapter.fullPath, info2.localIdx, chapter.images.length);
      } else if(currentFolder && window.ReadingProgress){
        window.ReadingProgress.saveDebounced(currentFolder, idx, images.length);
      }
      if(window.scheduleUrlSync) window.scheduleUrlSync();
    }
    ensureWindow(idx); evictStrip(idx); updateStripPositions();
    var gen=++window.snapGen;
    requestAnimationFrame(function(){
      if(gen!==window.snapGen) return;
      updateStripPositions();
      if(!animate){ rebaseAnchor(idx); return; }
      applyTrackTransform(idx,0,true);
      window.sliding=true;
      var settled=false;
      var track=document.getElementById("img-track");
      var settle=function(e){
        if(e && !isTrackSlideEnd(e)) return;
        if(settled || gen!==window.snapGen) return;
        settled=true; window.sliding=false;
        if(track) track.removeEventListener("transitionend", settle);
        rebaseAnchor(idx);
      };
      if(track) track.addEventListener("transitionend", settle);
      setTimeout(settle,380);
    });
    // spinner hide
    var stripImgs=window.stripImgs||{};
    var cur=stripImgs[idx];
    var imgContainer=document.getElementById("img-container");
    var spinner=document.getElementById("spinner");
    if(cur){
      var onCurError=function(){
        if(idx!==window.currentIdx) return;
        if(cur.naturalWidth!==0) return;
        var getUrlFn=window.getUrl||getUrl;
        if(cur.getAttribute("src")!==getUrlFn(window.currentIdx)) return;
        if(imgContainer) imgContainer.classList.remove("loading");
        if(spinner) spinner.style.display="none";
        var displayIdx=(window.globalToRealIdx && window.globalToRealIdx[idx]!==undefined? window.globalToRealIdx[idx]:idx)+1;
        var total=chapterList.length>1? (window.realImageCount||images.length):images.length;
        if(counter) counter.textContent=displayIdx+" / "+total+" \u26A0";
      };
      var done=function(){
        if(idx===window.currentIdx){
          if(imgContainer) imgContainer.classList.remove("loading");
          if(spinner) spinner.style.display="none";
        }
      };
      if(cur.complete && cur.naturalWidth>0) done();
      else {
        cur.addEventListener("load", done, {once:true});
        cur.addEventListener("error", onCurError, {once:true});
        if(cur.complete && cur.classList.contains("loaded")) onCurError();
      }
    }
  }
  window.rebuildSpreads=rebuildSpreads; window.spreadOf=spreadOf; window.arOf=arOf;
  window.spreadMetrics=spreadMetrics; window.gapBefore=gapBefore; window.spreadLeft=spreadLeft;
  window.pageOffsetPx=pageOffsetPx; window.ensureSpreadEl=ensureSpreadEl; window.placeCell=placeCell;
  window.regroupCells=regroupCells; window.recordAR=recordAR; window.ensureStripImage=ensureStripImage;
  window.ensureWindow=ensureWindow; window.evictStrip=evictStrip; window.isTrackSlideEnd=isTrackSlideEnd;
  window.applyTrackTransform=applyTrackTransform; window.updateStripPositions=updateStripPositions;
  window.rebaseAnchor=rebaseAnchor; window.snapTo=snapTo;
  window.setTrackOffset=function(idx,dragPx){
    var track=document.getElementById("img-track");
    if(track) track.style.transition="none";
    updateStripPositions();
    applyTrackTransform(idx,dragPx,false);
  };
  window.goNext=function(){
    var images=_getImages(); if(!images.length) return;
    var s=spreadOf(window.currentIdx||0);
    var spreads=window.spreads||[];
    if(s+1<spreads.length) snapTo(spreads[s+1].pages[0]);
  };
  window.goPrev=function(){
    var images=_getImages(); if(!images.length) return;
    var s=spreadOf(window.currentIdx||0);
    if(s>0) snapTo(spreads[s-1].pages[0]);
  };
  window.SpreadView = {
    rebuildSpreads, spreadMetrics, spreadLeft, gapBefore, placeCell, regroupCells,
    ensureStripImage, ensureWindow, evictStrip, snapTo, pageOffsetPx,
    sliderMax, idxToSlider, sliderToIdx, syncSliderValue, updateStripPositions, rebaseAnchor,
    invalidateSpreadSizes, spreadOf, arOf,
    imagePool, GAP_INNER
  };

  // ── Orientation helpers (also spread-related) ──
  function updateOrientation(){
    window.viewW=window.innerWidth; window.viewH=window.innerHeight;
    invalidateSpreadSizes();
    var prevMode=window.pageMode;
    var isLandscape=window.viewW>=window.viewH;
    if(isLandscape && window.landscapeSpread){ window.pageMode='spread'; document.body.className='landscape'; }
    else if(isLandscape && !window.landscapeSpread){ window.pageMode='single'; document.body.className='landscape-single'; }
    else { window.pageMode='single'; document.body.className='portrait'; }
    if(window.updateSpreadToggleLabel) window.updateSpreadToggleLabel();
    var modeChanged=prevMode!==window.pageMode;
    var imgs=_getImages(); if(!imgs.length) return;
    if(modeChanged && window.pageMode==='spread') window.currentIdx=Math.floor((window.currentIdx||0)/2)*2;
    rebuildSpreads(); regroupCells(); updateStripPositions();
    var ps=document.getElementById("page-slider");
    if(ps){ ps.max=sliderMax(); }
    syncSliderValue(window.currentIdx||0);
    snapTo(window.currentIdx||0, false);
    if(window.updateShiftButtonStyle) window.updateShiftButtonStyle();
  }
  window.updateOrientation=updateOrientation;
  // window.addEventListener('resize', updateOrientation); // disabled - app handles
  // window.addEventListener('orientationchange', function(){ setTimeout(updateOrientation,150); }); // disabled // disabled function(){ setTimeout(updateOrientation,150); });

  // viewer swipe drag is owned by app.js — no duplicate handler here
})();
