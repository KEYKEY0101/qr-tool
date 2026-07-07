// 上傳前在瀏覽器端壓縮照片（縮到 1600px JPEG），大幅加快手機遠端上傳
async function compressPhoto(file) {
    try {
        if (!file.type.startsWith('image/')) return file;
        let bmp;
        try {
            // from-image：依照片的 EXIF 方向自動轉正
            bmp = await createImageBitmap(file, {imageOrientation: 'from-image'});
        } catch (e) {
            bmp = await createImageBitmap(file);
        }
        const MAX = 1600;
        const scale = Math.min(1, MAX / Math.max(bmp.width, bmp.height));
        const w = Math.round(bmp.width * scale);
        const h = Math.round(bmp.height * scale);
        const canvas = document.createElement('canvas');
        canvas.width = w;
        canvas.height = h;
        canvas.getContext('2d').drawImage(bmp, 0, 0, w, h);
        bmp.close && bmp.close();
        const blob = await new Promise(res => canvas.toBlob(res, 'image/jpeg', 0.82));
        if (!blob || blob.size >= file.size) return file;  // 壓不了就傳原檔
        const name = (file.name || 'photo').replace(/\.[^.]+$/, '') + '.jpg';
        return new File([blob], name, {type: 'image/jpeg'});
    } catch (e) {
        return file;  // 任何失敗都退回原檔，確保照樣能傳
    }
}

async function compressPhotos(fileList) {
    const out = [];
    for (const f of fileList) out.push(await compressPhoto(f));
    return out;
}

// ---------- 自動轉大寫（支援中文輸入法：速成/倉頡組字中不干擾） ----------
function bindUppercase(el) {
    const apply = () => {
        if (el.value === el.value.toUpperCase()) return;
        const pos = el.selectionStart;
        el.value = el.value.toUpperCase();
        try { el.setSelectionRange(pos, pos); } catch (e) { /* ignore */ }
    };
    el.addEventListener('input', e => {
        if (e.isComposing) return;  // 輸入法組字中，千萬不要動欄位
        apply();
    });
    el.addEventListener('compositionend', apply);  // 選完字才轉大寫
}

// ---------- 前端診斷回報（寫進伺服器日誌） ----------
function clientLog(msg) {
    try {
        const data = JSON.stringify({msg: msg});
        if (navigator.sendBeacon) {
            navigator.sendBeacon('/api/clientlog',
                new Blob([data], {type: 'application/json'}));
        } else {
            fetch('/api/clientlog', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: data
            }).catch(() => {});
        }
    } catch (e) { /* 回報失敗不影響功能 */ }
}
window.addEventListener('error', e => {
    clientLog(`JS錯誤: ${e.message} @${(e.filename || '').split('/').pop()}:${e.lineno || 0}`);
});
window.addEventListener('unhandledrejection', e => {
    clientLog('Promise錯誤: ' + ((e.reason && e.reason.message) || String(e.reason)).slice(0, 200));
});

// ---------- 帶逾時的上傳（避免卡住無回應） ----------
async function uploadWithTimeout(url, opts, ms = 90000) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), ms);
    try {
        return await fetch(url, {...opts, signal: ctrl.signal});
    } catch (err) {
        if (err.name === 'AbortError') {
            throw new Error('上傳逾時（90秒），請檢查連線後再試');
        }
        throw err;
    } finally {
        clearTimeout(timer);
    }
}
