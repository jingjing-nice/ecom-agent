import { AnalyticsResults } from './panels';
import {Activity,ArrowDown,ArrowRight,ArrowUp,ArrowUpRight,Bag,Chart,Plus,Refresh,Search,Spinner,Tag,Trash,X} from './icons';
import React, {useEffect, useRef, useState} from 'react';

type Data = Record<string, any>;
type Props = {api:(path:string, body?:unknown, headers?:Record<string,string>)=>Promise<any>; token:string; role:string; onSignOut:()=>void};
const statusLabel: Record<string,string> = {DRAFT:'待确认',SUBMITTED:'已提交',REVIEWING:'系统核验中',NEED_MORE_INFO:'待补充材料',APPROVED:'审核通过',CREATING:'正在创建',CREATED:'售后单已创建',UNKNOWN:'正在核对',REJECTED:'未通过',CANCELLED:'已取消'};

export function BuyerChat({api, token, role, onSignOut}:Props) {
  const [conversations,setConversations]=useState<Data[]>([]);
  const [conversation,setConversation]=useState('');
  const [deleteTarget,setDeleteTarget]=useState<Data|null>(null);
  const deleteDialog=useRef<HTMLDialogElement>(null);
  useEffect(()=>{if(deleteTarget)deleteDialog.current?.showModal();else deleteDialog.current?.close();},[deleteTarget]);
  const [messages,setMessages]=useState<Data[]>([]);
  const [input,setInput]=useState('');
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const [orders,setOrders]=useState<Data[]>([]);
  const [itemId,setItemId]=useState('');
  const [size,setSize]=useState('');
  const [panel,setPanel]=useState<'orders'|'aftersale'|'image'|null>(null);
  const [caseType,setCaseType]=useState('refund');
  const [reason,setReason]=useState('');
  const [cases,setCases]=useState<Data[]>([]);
  const [caseInfo,setCaseInfo]=useState<Data|null>(null);
  const [confirmed,setConfirmed]=useState(false);
  const [imageKind,setImageKind]=useState('product_image');
  const [image,setImage]=useState<Data|null>(null);
  const [before,setBefore]=useState<number|null>(null);
  const [showJump,setShowJump]=useState(false);
  const inputRef=useRef<HTMLTextAreaElement>(null);
  const bottom=useRef<HTMLDivElement>(null);
  const controller=useRef<AbortController|null>(null);
  const followLatest=useRef(true);
  const sending=useRef(false);
  const typeQueue=useRef(new Map<string,{text:string;shown:number;done:boolean;after?:Data}>());
  const typeTimer=useRef<number|null>(null);
  const selected=orders.flatMap(o=>o.items.map((i:Data)=>({...i,order_no:o.order_no}))).find(i=>i.id===itemId);
  async function run(action:()=>Promise<void>) {setBusy(true);setError('');try{await action();}catch(e){if(!(e instanceof DOMException && e.name==='AbortError')) setError(e instanceof Error?e.message:'操作失败，请重试');}finally{setBusy(false);}}
  async function refreshList(){setConversations((await api('/conversations')).items);}
  function handleScroll(e:React.UIEvent<HTMLDivElement>){const el=e.currentTarget;const nearBottom=el.scrollHeight-el.scrollTop-el.clientHeight<120;followLatest.current=nearBottom;setShowJump(!nearBottom);}
  function jumpToLatest(){followLatest.current=true;bottom.current?.scrollIntoView({behavior:'smooth'});setShowJump(false);}
  function growInput(el:HTMLTextAreaElement){el.style.height='auto';el.style.height=Math.min(el.scrollHeight,120)+'px';}
  useEffect(()=>{let active=true;Promise.all([api('/conversations'),api('/orders'),role==='buyer'?api('/after-sales'):Promise.resolve({items:[]})]).then(([c,o,a])=>{if(active){setConversations(c.items);setOrders(o.items);setCases(a.items);}}).catch(e=>{if(active)setError(e.message);});return()=>{active=false;controller.current?.abort();if(typeTimer.current!==null){clearInterval(typeTimer.current);typeTimer.current=null;}};},[]);
  useEffect(()=>{if(followLatest.current)bottom.current?.scrollIntoView({behavior:'smooth'});},[messages]);
  useEffect(()=>{if(!caseInfo || !['SUBMITTED','REVIEWING','APPROVED','CREATING','UNKNOWN'].includes(caseInfo.status))return;let active=true;const timer=setInterval(()=>{api(`/after-sales/${caseInfo.id}`).then(data=>{if(active)setCaseInfo(data);}).catch(()=>{});},2000);return()=>{active=false;clearInterval(timer);};},[caseInfo?.id,caseInfo?.status]);
  useEffect(()=>{if(!image || image.status!=='extracting')return;let active=true;const timer=setInterval(()=>{api(`/uploads/${image.id}`).then(data=>{if(active)setImage(data);}).catch(()=>{});},2000);return()=>{active=false;clearInterval(timer);};},[image?.id,image?.status]);
  async function loadConversation(id:string,older?:number){followLatest.current=!older;const data=await api(`/conversations/${id}/messages${older?`?before=${older}`:''}`);setConversation(id);setMessages(old=>older?[...data.items,...old]:data.items);setBefore(data.next_before);}
  function newChat(){followLatest.current=true;setConversation('');setMessages([]);setBefore(null);setInput('');setItemId('');setSize('');setImage(null);setError('');}
  async function deleteConversation(id:string){
    await run(async()=>{
      const response=await fetch(`/v1/conversations/${id}`,{method:'DELETE',headers:{Authorization:`Bearer ${token}`}});
      if(!response.ok){const data=await response.json();throw new Error(data.message||'删除失败，请重试');}
      if(conversation===id)newChat();
      setConversations(old=>old.filter(c=>c.id!==id));
      setDeleteTarget(null);
    });
  }
  function patchMessage(id:string,patch:Data){setMessages(old=>old.map(m=>m.id===id?{...m,...patch}:m));}
  function finishType(id:string,after?:Data){if(after)patchMessage(id,after);typeQueue.current.delete(id);}
  // 打字机：SSE 增量先进入目标缓冲，再逐字揭示；积压越多揭示越快，降级动效时直接整段上屏。
  function enqueueType(id:string,text:string,done:boolean,after?:Data){
    if(window.matchMedia('(prefers-reduced-motion: reduce)').matches){patchMessage(id,{content:text,...(after||{})});typeQueue.current.delete(id);return;}
    const item=typeQueue.current.get(id)||{text:'',shown:0,done:false};
    item.text=text;item.done=done;if(after)item.after=after;
    typeQueue.current.set(id,item);
    if(typeTimer.current===null)typeTimer.current=window.setInterval(()=>{
      typeQueue.current.forEach((t,mid)=>{
        if(t.shown>=t.text.length){if(t.done)finishType(mid,t.after);return;}
        t.shown+=Math.max(1,Math.ceil((t.text.length-t.shown)/16));
        patchMessage(mid,{content:t.text.slice(0,t.shown)});
      });
      if(!typeQueue.current.size&&typeTimer.current!==null){clearInterval(typeTimer.current);typeTimer.current=null;}
    },16);
  }
  async function send(text=input){if(!text.trim()||busy||sending.current)return;sending.current=true;followLatest.current=true;const content=text.trim();setInput('');if(inputRef.current)inputRef.current.style.height='auto';const userId=crypto.randomUUID(),replyId=crypto.randomUUID();setMessages(old=>[...old,{id:userId,role:'user',content},{id:replyId,role:'assistant',content:'',pending:true,trace:[]}]);
    await run(async()=>{try{const id=conversation||(await api('/conversations',{})).id;setConversation(id);const abort=new AbortController();controller.current=abort;
      const response=await fetch('/v1/chat/stream',{method:'POST',signal:abort.signal,headers:{'Content-Type':'application/json',Authorization:`Bearer ${token}`},body:JSON.stringify({conversation_id:id,client_message_id:userId,message:content,inherit_context:true,...(itemId?{order_item_id:itemId}:{}),...(size?{target_size:size}:{}),...(image?.status==='extracted'&&image.kind==='product_image'?{image_id:image.id}:{})})});
      if(!response.ok)throw new Error((await response.json()).message||'消息发送失败');
      const reader=response.body!.getReader(),decoder=new TextDecoder();let buffer='',reply='',trace:Data[]=[],completed=false;
      while(true){const {value,done}=await reader.read();buffer+=decoder.decode(value,{stream:!done});let end;
        while((end=buffer.indexOf('\n\n'))>=0){const block=buffer.slice(0,end);buffer=buffer.slice(end+2);const event=block.split('\n').find(l=>l.startsWith('event:'))?.slice(6).trim();const raw=block.split('\n').filter(l=>l.startsWith('data:')).map(l=>l.slice(5).trim()).join('\n');if(!raw)continue;const data=JSON.parse(raw);
          if(event==='error')throw new Error(data.message);
          if(event==='answer.delta'){reply+=data.text||'';enqueueType(replyId,reply,false);}
          if(event==='workflow.progress'){trace=[...trace,data];patchMessage(replyId,{trace});}
          if(event==='answer.completed'){completed=true;patchMessage(userId,{meta:{request:{client_message_id:userId},answer:data}});enqueueType(replyId,data.reply||data.message,true,{meta:data,trace:data.trace||trace,pending:false});}
        }if(done)break;
      }if(!completed)throw new Error('连接已结束，未收到完整回复，请刷新会话记录后重试');await refreshList().catch(()=>setError('回复已完成，会话列表刷新失败，请稍后刷新列表。'));
    }catch(e){typeQueue.current.delete(replyId);patchMessage(replyId,{pending:false,failed:true});throw e;}finally{controller.current=null;sending.current=false;}});
  }
  async function approve(threadId:string,decision:'APPROVED'|'REJECTED'){
    await run(async()=>{
      const user=messages.find(m=>m.role==='user'&&m.meta?.answer?.thread_id===threadId);
      const original=user?.meta?.request;
      if(!original)throw new Error('请刷新会话后再次确认');
      await api('/chat/approve',{conversation_id:conversation,client_message_id:original.client_message_id,decision});
      await loadConversation(conversation);
    });
  }
  async function upload(file:File){if(file.size>10*1024*1024)throw new Error('请选择不超过 10MB 的图片');const ticket=await api('/uploads/presign',{file_name:file.name,kind:imageKind});const put=await fetch(ticket.upload_url,{method:'PUT',body:file,headers:{'Content-Type':file.type||'application/octet-stream'}});if(!put.ok)throw new Error('图片上传失败');await api(`/uploads/${ticket.image_id}/complete`,{});setImage({id:ticket.image_id,kind:imageKind,status:'extracting'});}
  const today=new Date();const year=today.getFullYear(),month=today.getMonth()+1;
  const quick=['500 元以内的吸顶灯',role==='ops'?'查询店铺最近的订单':'查询我的订单',role==='ops'?`按品类统计 ${year} 年 ${month} 月 1 日至 ${month===12?year+1:year} 年 ${month===12?1:month+1} 月 1 日的支付金额`:'这盏灯尺寸太小，没货就推荐同品牌替代款'];
  function renderCards(meta:Data){return <>{meta.cards?.length>0&&<div className="chat-products">{meta.cards.map((card:Data)=><div className="chat-product" key={card.sku_id}><div className="product-symbol">{card.category==='吸顶灯'?<Activity size={24}/>:card.category==='吊灯'?<Bag size={24}/>:<Tag size={24}/>}</div><small>{card.brand} · {card.category}</small><strong>{card.title}</strong><p><b>¥{(card.price_minor/100).toFixed(2)}</b><span>库存 {card.available_qty}</span></p><small>{Object.entries(card.specs||{}).map(([k,v])=>`${k} ${v}`).join(' · ')}</small><button className="quiet" disabled={busy} onClick={()=>run(async()=>{const data=await api(`/products/${encodeURIComponent(card.product_id)}`);setMessages(old=>[...old,{id:crypto.randomUUID(),role:'assistant',content:`${data.title}\n${data.description}\n${Object.entries(data.attributes||{}).map(([k,v])=>`${k}：${v}`).join('；')}`}]);})}>查看详情 <ArrowUpRight size={11}/></button></div>)}</div>}{meta.data?.kind==='orders'&&<div className="chat-order-results">{meta.data.orders.map((o:Data)=><div className="order-card" key={o.id}><strong>{o.order_no} · {({paid:'待发货',shipped:'已发货',delivered:'已签收',completed:'已完成'} as Record<string,string>)[o.status]||o.status}</strong><small>实付 ¥{(o.paid_amount_minor/100).toFixed(2)}</small>{o.items.map((i:Data)=><button key={i.id} disabled={busy} onClick={()=>{setItemId(i.id);setPanel('orders');}}>{i.title} · {i.quantity} 件<small>关联此商品继续咨询 <ArrowRight size={10}/></small></button>)}</div>)}</div>}{meta.data&&meta.data.kind!=='orders'&&<AnalyticsResults data={meta.data}/>} {meta.evidence?.length>0&&<details className="sources"><summary>查看 {meta.evidence.length} 条参考依据</summary>{meta.evidence.map((e:Data,i:number)=><blockquote key={e.chunk_id||i}>{e.content}<small>{e.doc_title} · {e.version}</small></blockquote>)}</details>}</>;}
  return <div className="chat-app">
    <dialog ref={deleteDialog} className="delete-dialog" aria-labelledby="delete-dialog-title" aria-describedby="delete-dialog-description" onCancel={e=>{e.preventDefault();if(!busy)setDeleteTarget(null);}}>
      <h2 id="delete-dialog-title">删除这条对话？</h2>
      <p className="delete-dialog-name">{deleteTarget?.title||'新的对话'}</p>
      <p id="delete-dialog-description">该对话及消息记录将被删除，无法恢复。</p>
      {error&&<p role="alert" className="failed-message">{error}</p>}
      <div className="delete-dialog-actions"><button autoFocus className="delete-cancel" disabled={busy} onClick={()=>setDeleteTarget(null)}>取消</button><button className="delete-confirm" disabled={busy||!deleteTarget} onClick={()=>deleteTarget&&deleteConversation(deleteTarget.id)}>{busy?'正在删除…':'确认删除'}</button></div>
    </dialog>
    <aside className="chat-sidebar"><a className="chat-brand" href="/"><span className="brand-mark">e</span><div>Ecom Assistant<small>你的企业智能助手</small></div></a><button className="new-chat" disabled={busy} onClick={newChat}><Plus size={14}/> 新建对话</button><div className="sidebar-label">最近对话 <button aria-label="刷新会话" disabled={busy} onClick={()=>run(refreshList)}><Refresh size={14}/></button></div><nav className="conversation-nav">{conversations.map(c=><div className={`conversation-row${conversation===c.id?' active':''}`} key={c.id}><button className="conversation-open" disabled={busy} onClick={()=>run(()=>loadConversation(c.id))}><span className="conversation-title">{c.title||'新的对话'}</span></button><button className="conversation-delete" disabled={busy} title="删除对话" aria-label={`删除对话：${c.title||'新的对话'}`} onClick={()=>{setError('');setDeleteTarget(c);}}><Trash size={14}/></button></div>)}{!conversations.length&&<p className="muted">开始一次对话，记录会出现在这里。</p>}</nav><div className="sidebar-bottom"><span className="user-chip"><i/>Ecom Assistant · {role==='ops'?'运营版':'购物顾问'}</span><button className="quiet" disabled={busy} onClick={onSignOut}>切换账号 <ArrowUpRight size={11}/></button></div></aside>
    <div className="chat-main"><div className="chat-topbar"><div><strong>智能客服</strong><span className="online-dot"/> <small>{role==='ops'?'商品 · 订单 · 运营分析':'导购 · 订单 · 售后服务'}</small></div><div className="top-actions"><button className={panel==='orders'?'active':''} disabled={busy} onClick={()=>setPanel(panel==='orders'?null:'orders')}>▤ {role==='ops'?'店铺订单':'我的订单'}</button>{role==='buyer'&&<button className={panel==='aftersale'?'active':''} disabled={busy} onClick={()=>setPanel(panel==='aftersale'?null:'aftersale')}>售后服务 <ArrowUpRight size={12}/></button>}</div></div>
      <div className="message-scroll" onScroll={handleScroll}><div className="message-column">{!messages.length&&<div className="chat-welcome"><div className="welcome-mark">✳</div><span className="welcome-kicker">让每一次购物更简单</span><h1>你好，有什么可以帮你？</h1><p>{role==='ops'?'了解商品，查询店铺订单，分析运营指标。':'挑选心仪商品，了解订单，或处理售后。'}<br/>把问题交给我，我们一步一步来。</p><div className="quick-prompts">{quick.map((q,i)=><button disabled={busy} key={q} onClick={()=>{setInput(q);if(i===2&&role==='buyer')setPanel('orders');}}><span>{i===0?<Search size={21}/>:i===1?<Bag size={21}/>:role==='ops'?<Chart size={21}/>:<Refresh size={21}/>}</span><strong>{['帮我挑选商品','查询订单状态',role==='ops'?'分析经营数据':'查询换货与替代'][i]}</strong><small>{q}</small><b><ArrowUpRight size={13}/></b></button>)}</div></div>}
        {before&&<button className="history-more" disabled={busy} onClick={()=>run(()=>loadConversation(conversation,before))}>加载更早消息</button>}
        {messages.map(m=><div className={`message-row ${m.role==='user'?'from-user':'from-assistant'}`} key={m.id}>{m.role!=='user'&&<div className="assistant-avatar">✳</div>}<div className="message-body"><small className="message-author">{m.role==='user'?'你':'Ecom 智能助手'}</small><div className={m.pending&&m.content?'message-text typing':'message-text'}>{m.content|| (m.pending?<span className="thinking">正在为你查询<span> · · ·</span></span>:m.failed?'本次回复未完成，请重试。':'')}</div>{m.meta&&renderCards(m.meta)}{m.meta?.requires_human_approval&&<div className="context-chips"><span>请确认本次咨询建议；售后申请需另行提交。</span><button disabled={busy} onClick={()=>approve(m.meta.thread_id,'APPROVED')}>确认建议</button><button disabled={busy} onClick={()=>approve(m.meta.thread_id,'REJECTED')}>取消</button></div>}{m.meta?.approval&&<small>{m.meta.approval.decision==='APPROVED'?'已确认咨询建议':'已取消确认'}</small>}{(m.trace||m.meta?.trace)?.length>0&&<details className="trace-details"><summary>{m.pending?'正在处理':'处理过程'} · {(m.trace||m.meta.trace).length} 步</summary>{(m.trace||m.meta.trace).map((t:Data,i:number)=><p key={i}>{t.agent} · {t.tool}<small>{t.duration_ms} ms · {t.status}</small></p>)}</details>}{m.failed&&<small className="failed-message">回复中断，可刷新会话记录确认后重新发送。</small>}</div></div>)}<div ref={bottom}/></div></div>
      {showJump&&<button className="jump-latest" type="button" onClick={jumpToLatest}><ArrowDown size={13}/> 回到最新对话</button>}
      <div className="composer-area">{error&&<div className="chat-error" role="alert">{error}<button aria-label="关闭提示" onClick={()=>setError('')}><X size={14}/></button></div>}<form className="chat-composer" onSubmit={e=>{e.preventDefault();send();}}>{(selected||image)&&<div className="context-chips">{selected&&<span>▤ {selected.order_no} · {selected.title}<button type="button" disabled={busy} aria-label="清除关联订单" onClick={()=>{setItemId('');setSize('');}}><X size={10}/></button></span>}{image&&<span>▧ {image.kind==='product_image'?'商品图片':'售后凭证'} · {image.status==='extracted'?'识别完成':'待识别'}<button type="button" disabled={busy} aria-label="移除图片" onClick={()=>setImage(null)}><X size={10}/></button></span>}</div>}<textarea aria-label="聊天消息" rows={2} ref={inputRef} value={input} onChange={e=>{setInput(e.target.value);growInput(e.target);}} placeholder="告诉我你的需求，例如：想买盏 500 元以内的吸顶灯…" onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.nativeEvent.isComposing){e.preventDefault();send();}}}/><div className="composer-toolbar"><div>{role==='buyer'&&<button type="button" disabled={busy} onClick={()=>setPanel(panel==='image'?null:'image')}><Plus size={12}/> 图片 / 凭证</button>}<span>Enter 发送 · Shift + Enter 换行</span></div><button className="send-message" type="submit" disabled={busy||!input.trim()} aria-label="发送消息">{busy?<Spinner size={15}/>:<ArrowUp size={16}/>}</button></div></form><p className="composer-note">AI 提供购物与政策参考，价格库存以查询结果为准。售后操作需要你的明确确认。</p></div>
    </div>
    {panel&&<aside className="context-panel"><div className="panel-heading"><h2>{panel==='orders'?(role==='ops'?'授权店铺订单':'我的订单'):panel==='aftersale'?'售后服务':'图片与凭证'}</h2><button className="quiet" aria-label="关闭侧栏" onClick={()=>setPanel(null)}><X size={16}/></button></div>
      {panel==='orders'&&<><p className="muted">选择订单后，助手可以结合订单回答问题。</p><button className="quiet" disabled={busy} onClick={()=>run(async()=>setOrders((await api('/orders')).items))}>刷新订单 <Refresh size={12}/></button>{orders.map(o=><div className="order-card" key={o.id}><small>{o.order_no} · {o.status==='paid'?'待发货':o.status==='delivered'?'已签收':o.status}</small>{o.items.map((i:Data)=><button disabled={busy} className={itemId===i.id?'selected':''} key={i.id} onClick={()=>{setItemId(i.id);setCaseInfo(null);setConfirmed(false);}}><strong>{i.title}</strong><span>¥{(i.paid_amount_minor/100).toFixed(2)} · {i.specs?.size||'常规规格'}</span><small>{itemId===i.id?'✓ 已关联到对话':'点击关联到对话'}</small></button>)}</div>)}<label>目标尺寸（厘米，换货咨询）<input placeholder="例如 42（厘米）" value={size} onChange={e=>setSize(e.target.value)}/></label>{role==='buyer'&&<button disabled={!selected||busy} onClick={()=>{setPanel('aftersale');setReason(input);}}>为所选订单申请售后 <ArrowUpRight size={12}/></button>}</>}
      {panel==='image'&&<><p className="muted">上传商品图片寻找候选，或为售后补充凭证。需配置视觉模型服务。</p><label>图片用途<select disabled={busy} value={imageKind} onChange={e=>setImageKind(e.target.value)}><option value="product_image">识图选购</option><option value="refund_evidence">售后凭证</option></select></label><label className="upload-zone"><Plus size={13}/> 选择图片<small>JPG / PNG / WEBP · 最大 10 MB</small><input type="file" accept=".jpg,.jpeg,.png,.webp" disabled={busy} onChange={e=>{const f=e.target.files?.[0];if(f)run(()=>upload(f));e.target.value='';}}/></label>{image&&<><p>识别状态：{image.status}</p><button disabled={busy} onClick={()=>run(async()=>setImage(await api(`/uploads/${image.id}`)))}>刷新识别结果</button>{image.error&&<p role="alert">{image.error}</p>}{image.extracted&&<details><summary>查看识别字段</summary><pre>{JSON.stringify(image.extracted,null,2)}</pre></details>}</>}</>}
      {panel==='aftersale'&&<><p className="muted">系统按已发布政策自动核验，处理进度实时同步。</p><div className="selected-order">{selected?`${selected.order_no} · ${selected.title}`:'请先在「我的订单」中选择商品'}<button className="quiet" disabled={busy} onClick={()=>setPanel('orders')}>选择订单 <ArrowRight size={11}/></button></div><label>申请类型<select disabled={busy} value={caseType} onChange={e=>setCaseType(e.target.value)}><option value="refund">未发货退款</option><option value="return_refund">退货退款</option></select></label><label>申请原因<textarea value={reason} onChange={e=>setReason(e.target.value)} placeholder="描述你遇到的问题…"/></label><button disabled={busy||!itemId||!reason.trim()} onClick={()=>run(async()=>{const data=await api('/after-sales/drafts',{order_item_id:itemId,case_type:caseType,reason,evidence_ids:image?.kind==='refund_evidence'&&image.status==='extracted'?[image.id]:[]});setCaseInfo(data);setConfirmed(false);})}>生成申请草稿</button>{caseInfo&&<div className="case-status"><span className="status-pill">{statusLabel[caseInfo.status]||caseInfo.status}</span><p>{caseInfo.review_comment||caseInfo.reason}</p>{caseInfo.last_error&&<p className="failed-message">{caseInfo.last_error}</p>}{caseInfo.external_case_id&&<small>外部工单号：{caseInfo.external_case_id}</small>}{caseInfo.status==='DRAFT'&&<><label className="check"><input type="checkbox" checked={confirmed} onChange={e=>setConfirmed(e.target.checked)}/>我已核对并确认提交</label><button disabled={busy||!confirmed} onClick={()=>run(async()=>setCaseInfo(await api(`/after-sales/${caseInfo.id}/submit`,{expected_version:caseInfo.version,confirmed:true},{'Idempotency-Key':`${caseInfo.id}-submit`})))}>确认提交</button></>}{caseInfo.status==='NEED_MORE_INFO'&&<button disabled={busy||!reason.trim()} onClick={()=>run(async()=>setCaseInfo(await api(`/after-sales/${caseInfo.id}/supplement`,{expected_version:caseInfo.version,comment:reason,evidence_ids:image?.kind==='refund_evidence'&&image.status==='extracted'?[image.id]:[]})))}>补充材料并重新核验</button>}{['DRAFT','SUBMITTED','REVIEWING','NEED_MORE_INFO'].includes(caseInfo.status)&&<button className="quiet" disabled={busy} onClick={()=>run(async()=>setCaseInfo(await api(`/after-sales/${caseInfo.id}/cancel`,{expected_version:caseInfo.version,reason:'用户主动取消申请'})))}>取消申请</button>}<button className="quiet" disabled={busy} onClick={()=>run(async()=>setCaseInfo(await api(`/after-sales/${caseInfo.id}`)))}>刷新状态 <Refresh size={12}/></button></div>}<div className="past-cases"><h3>已有申请</h3><button className="quiet" disabled={busy} onClick={()=>run(async()=>setCases((await api('/after-sales')).items))}>刷新列表</button>{cases.map(c=><button key={c.id} disabled={busy} onClick={()=>run(async()=>{setConfirmed(false);const data=await api(`/after-sales/${c.id}`);setCaseInfo(data);setItemId(data.order_item_id);setReason(data.reason);setCaseType(data.case_type);})}><span>{statusLabel[c.status]||c.status}</span><small>{c.reason}</small></button>)}</div></>}
    </aside>}
  </div>;
}
