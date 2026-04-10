const ScanCard = (() => {
  let _stream = null;
 
  // ── Open camera ─────────────────────────────────────────────────────────────
  async function open() {
    const overlay = document.getElementById('scannerOverlay');
    const video   = document.getElementById('scannerVideo');
    if (!overlay || !video) {
      console.error('ScanCard: missing #scannerOverlay or #scannerVideo in DOM');
      return;
    }
    overlay.style.display = 'flex';
    try {
      _stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: 'environment', width: { ideal: 1280 }, height: { ideal: 720 } }
      });
      video.srcObject = _stream;
    } catch (err) {
      ScanCard.close();
      if (window.ScanCard_onError) window.ScanCard_onError('Camera access denied. Please check browser permissions.');
      else alert('Camera access denied.');
    }
  }
 
  // ── Stop camera ──────────────────────────────────────────────────────────────
  function close() {
    if (_stream) { _stream.getTracks().forEach(t => t.stop()); _stream = null; }
    const overlay = document.getElementById('scannerOverlay');
    if (overlay) overlay.style.display = 'none';
  }
 
  // ── Capture frame and send to backend ────────────────────────────────────────
  async function capture() {
    const video  = document.getElementById('scannerVideo');
    const canvas = document.getElementById('scannerCanvas');
    if (!video || !canvas) return;
 
    canvas.width  = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
 
    canvas.toBlob(blob => {
      close();
      _process(blob);
    }, 'image/jpeg', 0.95);
  }
 
  // ── Send to /leads/scan-card ──────────────────────────────────────────────────
  async function _process(blob) {
    const api   = window.SF_API   || 'http://localhost:8000/api';
    const token = window.SF_TOKEN || localStorage.getItem('sf_token');
 
    if (window.ScanCard_onLoading) window.ScanCard_onLoading(true);
 
    const form = new FormData();
    form.append('file', blob, 'business_card.jpg');
 
    try {
      const r = await fetch(`${api}/leads/scan-card`, {
        method:  'POST',
        headers: { 'Authorization': 'Bearer ' + token },
        body:    form,
      });
 
      if (window.ScanCard_onLoading) window.ScanCard_onLoading(false);
 
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        const msg = err.detail || 'Failed to read card. Try a clearer photo.';
        if (window.ScanCard_onError) window.ScanCard_onError(msg);
        return;
      }
 
      const data = await r.json();
      if (window.ScanCard_onSuccess) window.ScanCard_onSuccess(data);
 
    } catch (e) {
      if (window.ScanCard_onLoading) window.ScanCard_onLoading(false);
      if (window.ScanCard_onError)   window.ScanCard_onError('Network error: ' + e.message);
    }
  }
 
  return { open, close, capture };
})();