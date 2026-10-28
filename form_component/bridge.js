// Transporta as operações do formulário original pela sessão do Streamlit.
(() => {
  let sequence = 0;
  const queue = [];
  let pending = null;
  let bootstrap;
  let handshake;
  let heightFrame = null;
  let lastHeight = 0;
  let componentReady = false;
  let resolveReady;
  const ready = new Promise(resolve => { resolveReady = resolve; });
  const send = (type, extra = {}) => window.parent.postMessage({isStreamlitMessage:true,type,...extra}, '*');
  function resizeToContent() {
    if (!componentReady || heightFrame !== null) return;
    heightFrame = requestAnimationFrame(() => {
      heightFrame = null;
      if (!document.body) return;
      // Medir o corpo, e não o viewport do iframe, permite crescer e encolher
      // ao trocar de etapa, sem manter uma segunda área de rolagem.
      const height = Math.ceil(document.body.getBoundingClientRect().height) + 2;
      if (height > 0 && height !== lastHeight) {
        lastHeight = height;
        send('streamlit:setFrameHeight',{height});
      }
    });
  }
  function observeContent() {
    if (!document.body) return;
    if (typeof ResizeObserver !== 'undefined') new ResizeObserver(resizeToContent).observe(document.body);
    if (typeof MutationObserver !== 'undefined') new MutationObserver(resizeToContent).observe(document.body,{childList:true,subtree:true,attributes:true,characterData:true});
    document.addEventListener('load',resizeToContent,true);
    window.addEventListener('resize',resizeToContent);
    document.fonts?.ready.then(resizeToContent);
    resizeToContent();
  }
  function pump() {
    if (pending || !queue.length) return;
    pending = queue.shift();
    send('streamlit:setComponentValue', {value:pending.message,dataType:'json'});
  }
  window.addEventListener('message', event => {
    if (event.source !== window.parent || event.data?.type !== 'streamlit:render') return;
    clearInterval(handshake);
    componentReady = true;
    resizeToContent();
    const args = event.data.args || {};
    if (args.bootstrap) { bootstrap = args.bootstrap; resolveReady(); }
    const reply = args.reply;
    if (pending && reply?.id === pending.message.id) {
      const current = pending; pending = null;
      if (reply.error) current.reject(new Error(reply.error));
      else current.resolve(reply.result);
      pump();
    }
  });
  window.portalRequest = async (url, options = {}) => {
    await ready;
    if (url === '/api/bootstrap') return bootstrap;
    let body = options.body ? JSON.parse(typeof options.body === 'string' ? options.body : '{}') : {};
    if (options.body instanceof FormData) {
      const file = options.body.get('file');
      if (!file || file.size > 10 * 1024 * 1024) throw new Error('Cada anexo deve ter até 10 MB.');
      body = {filename:file.name,content_b64:await new Promise((resolve,reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).split(',')[1]);
        reader.onerror = reject;reader.readAsDataURL(file);
      })};
    }
    return new Promise((resolve,reject) => {
      queue.push({message:{id:`${Date.now()}-${++sequence}`,url,method:options.method || 'GET',body},resolve,reject});pump();
    });
  };
  const announce = () => send('streamlit:componentReady',{apiVersion:1});
  // A página pode carregar antes que o Streamlit registre o listener do iframe.
  // Repetir o handshake até o primeiro render evita deixar o componente invisível.
  handshake=setInterval(announce,250);
  announce();
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded',observeContent,{once:true});
  else observeContent();
})();
