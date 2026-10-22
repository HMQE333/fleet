// Adapted from jev-ultrafast (c) 2026 Browser Use, MIT — see fleet/fastbrowser/NOTICE.md.
// Fleet changes: payment / one-time-code fields are never offered (RULES.md §3), links carry
// their href (for noise filtering and domain checks), `login` flags a visible password field,
// file inputs are offered as `upload` (the operator picks the file, never the page or the model),
// and same-origin frames are read like the page around them (their controls carry the frame's
// offset; a cross-origin frame is left alone: its document can't be reached from here).
(() => {
  if (!document.body) return null;
  const cache = window.__jevFast ||= {ids:new WeakMap(), nodes:new Map(), next:1};
  const identity = e => {
    if (!cache.ids.has(e)) cache.ids.set(e,cache.next++);
    const id=cache.ids.get(e); cache.nodes.set(id,e); return id;
  };
  for (const [id,e] of cache.nodes) if (!e.isConnected) cache.nodes.delete(id);
  const secretField = e => /^(cc-|one-time-code)/.test(e.getAttribute('autocomplete')||'') ||
    /(^|[-_])(cvv|cvc|card[-_]?(number|num)|cc[-_]?(number|num|exp))([-_]|$)/i.test((e.name||'')+' '+(e.id||''));
  const safe = e => !['password','hidden'].includes(e.type) && !secretField(e);
  const visible = e => !e.closest('[aria-hidden="true"],[inert]') &&
    e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  const name = (e,seen=new Set()) => {
    if (!e || seen.has(e)) return '';
    seen.add(e);
    const referenced=(e.getAttribute('aria-labelledby')||'').split(/\s+/)
      .map(id=>name(e.ownerDocument.getElementById(id),seen)).filter(Boolean).join(' ');
    return referenced || e.getAttribute('aria-label') ||
      [...(e.labels||[])].map(l=>name(l,seen)).filter(Boolean).join(' ') ||
      (['button','submit','reset'].includes(e.type) ? e.value : '') || e.getAttribute('alt') ||
      (e.tagName==='INPUT' ? '' : [...e.childNodes].map(n=>n.nodeType===3 ? n.textContent :
        n.nodeType===1 && n.getAttribute('aria-hidden')!=='true' ? name(n,seen) : '').join(' ').trim()) ||
      e.getAttribute('title') || e.getAttribute('placeholder') || '';
  };
  const roles=['button','link','checkbox','radio','switch','tab','menuitem','menuitemradio',
    'option','gridcell','combobox','textbox','searchbox','spinbutton'];
  const selector='a[href],button,input,textarea,select,summary,[contenteditable="true"],'+
    roles.map(role=>'[role="'+role+'"]').join(',');
  const role = e => {
    const explicit=e.getAttribute('role');
    if (roles.includes(explicit)) return explicit;
    if (e.tagName==='BUTTON' || e.tagName==='SUMMARY') return 'button';
    if (e.tagName==='A') return 'link';
    if (e.tagName==='SELECT') return 'combobox';
    if (e.tagName==='TEXTAREA' || e.isContentEditable) return 'textbox';
    if (e.tagName==='INPUT') {
      if (['checkbox','radio'].includes(e.type)) return e.type;
      if (['button','submit','reset','image'].includes(e.type)) return 'button';
      if (e.type==='file') return 'upload';
      if (e.type==='search') return 'searchbox';
      if (e.type==='number') return 'spinbutton';
      if (['text','email','url','tel'].includes(e.type)) return 'textbox';
    }
    return null;
  };
  // The page and every same-origin frame in it (three levels deep), each with the offset of its viewport in the
  // window. A frame whose document can't be reached (cross-origin, sandboxed) or isn't on screen is not entered.
  const frameDoc = f => { try { return f.contentDocument; } catch (_) { return null; } };
  const docs = (doc,ox,oy,depth,out) => {
    if (!doc?.body) return out;
    out.push({doc,win:doc.defaultView,ox,oy});
    if (depth>=3) return out;
    for (const f of doc.querySelectorAll('iframe,frame')) {
      const inner=frameDoc(f), r=f.getBoundingClientRect();
      if (inner && visible(f) && r.width>0 && r.height>0)
        docs(inner,ox+r.x+f.clientLeft,oy+r.y+f.clientTop,depth+1,out);
    }
    return out;
  };
  cache.docs=()=>docs(document,0,0,0,[]).map(d=>d.doc);
  cache.pageKey=()=>[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    cache.docs().flatMap(d=>[...d.querySelectorAll('input,textarea,select')]).filter(safe)
      .map(e=>[identity(e),e.value,e.checked,e.selectedIndex,e.disabled,e.readOnly])];
  cache.guard=e=>{
    if (!e?.isConnected || !visible(e)) return null;
    const scope=e.closest('form,dialog,[role="dialog"],article,li,tr,[role="row"]') || e.parentElement;
    return [identity(e),role(e),name(e),e.value??null,e.checked??null,e.selectedIndex??null,
      e.readOnly??null,e.matches(':disabled'),e.getAttribute('aria-disabled'),
      e.getAttribute('aria-expanded'),e.getAttribute('aria-checked'),e.getAttribute('aria-selected'),
      e.getAttribute('href'),scope?.innerText?.slice(0,6000)||''];
  };
  // Enough of the element shows, both in its own frame and in the window around it, for input to aim at it: a
  // control cut by the fold is still offered, because page.py aims at the centre of the part that is on screen
  // (and hit-tests that point) rather than at the element's own centre. `r` is frame-local, so the window's
  // edges sit at -ox / innerWidth-ox; a small element entirely on screen is never dropped for its size.
  const MIN_VISIBLE=4;
  const onscreen = (r,ox,oy,win) => {
    const x0=Math.max(r.x,0,-ox), x1=Math.min(r.right,win.innerWidth,innerWidth-ox);
    const y0=Math.max(r.y,0,-oy), y1=Math.min(r.bottom,win.innerHeight,innerHeight-oy);
    return x1-x0>=Math.min(MIN_VISIBLE,r.width) && y1-y0>=Math.min(MIN_VISIBLE,r.height);
  };
  const actions=[], words=[];
  let length=0, login=false;
  for (const {doc,win,ox,oy} of docs(document,0,0,0,[])) {
    for (const e of doc.querySelectorAll(selector)) {
      if (!safe(e) || !visible(e) || e.matches(':disabled') || e.closest('[aria-disabled="true"]')) continue;
      const r=e.getBoundingClientRect(), rname=role(e);
      if (!rname || r.width<=0 || r.height<=0 || !onscreen(r,ox,oy,win)) continue;
      if (rname==='gridcell' && e.querySelector('button,[role="button"]')) continue;
      const base={node:identity(e),role:rname,label:name(e)||rname,
        rect:{x:ox+r.x,y:oy+r.y,w:r.width,h:r.height}};
      if (e.tagName==='A' && e.getAttribute('href')) base.href=e.href;
      for (const key of ['checked','selected','expanded']) {
        const value=e.getAttribute('aria-'+key);
        if (value!==null) base[key]=value;
      }
      if (['checkbox','radio'].includes(e.type)) base.checked=String(e.checked);
      if (rname==='upload') {
        actions.push({...base,kind:'upload',value:[...e.files].map(f=>f.name).join(', ')});
      } else if (e.tagName==='SELECT') {
        for (const o of e.options) if (!o.selected && !o.disabled && !o.closest('optgroup[disabled]'))
          actions.push({...base,kind:'select',value:o.value,
            current_value:[...e.selectedOptions].map(o=>o.label).join(', '),label:base.label+' → '+o.label});
      } else {
        const editable=!e.readOnly && e.getAttribute('aria-readonly')!=='true' &&
          (['textbox','searchbox','spinbutton'].includes(rname) ||
            (rname==='combobox' && ['INPUT','TEXTAREA'].includes(e.tagName)));
        const value='value' in e ? String(e.value) :
          e.isContentEditable || rname==='combobox' ? e.innerText.trim() : '';
        actions.push({...base,kind:editable?'fill':'click',value});
        if (editable) actions.push({...base,kind:'click',value,label:'Open '+base.label});
      }
    }
    const walker=doc.createTreeWalker(doc.body,NodeFilter.SHOW_TEXT), range=doc.createRange();
    let node;
    while ((node=walker.nextNode()) && length<6000) {
      const value=node.textContent.trim(), parent=node.parentElement;
      if (!value || !parent || parent.closest('script,style,noscript,template') || !visible(parent)) continue;
      range.selectNodeContents(node); const r=range.getBoundingClientRect();
      if (r.width>0 && r.height>0 && r.bottom>0 && r.top<win.innerHeight && r.right>0 && r.left<win.innerWidth &&
          oy+r.bottom>0 && oy+r.top<innerHeight && ox+r.right>0 && ox+r.left<innerWidth) {
        words.push(value); length+=value.length;
      }
    }
    login ||= [...doc.querySelectorAll('input[type="password"]')].some(e=>visible(e) &&
      e.getBoundingClientRect().width>0);
  }
  const text=words.join('\n').slice(0,6000), height=document.documentElement.scrollHeight;
  const page_key=cache.pageKey(), guards={};
  for (const a of actions) if (!(a.node in guards)) guards[a.node]=cache.guard(cache.nodes.get(a.node));
  // Compare meaning and identity. Geometry is always resolved and hit-tested just before input.
  const semantics=actions.map(({rect,...action})=>action);
  const marker=[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    document.title,text,semantics,page_key[6]];
  const omitted_actions=Math.max(0,actions.length-250);
  actions.splice(250);
  actions.forEach((a,i)=>a.id='e'+(i+1));
  if (scrollY+innerHeight<height-2) actions.push({id:'scroll_down',kind:'scroll',label:'Scroll down',delta:560});
  if (scrollY>0) actions.push({id:'scroll_up',kind:'scroll',label:'Scroll up',delta:-560});
  actions.push({id:'wait',kind:'wait',label:'Wait for the page to update'});
  return {url:location.href,title:document.title,w:innerWidth,h:innerHeight,text,login,
    scroll:{y:scrollY,height},actions,marker,page_key,guards,omitted_actions};
})()
