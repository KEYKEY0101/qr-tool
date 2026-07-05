// 手機下拉重新整理（頁面在最頂端時往下拉）
(function () {
    if (!('ontouchstart' in window)) return;  // 只在觸控裝置啟用

    // 避免與 Chrome 內建的下拉重新整理重複觸發
    document.documentElement.style.overscrollBehaviorY = 'contain';
    document.body.style.overscrollBehaviorY = 'contain';

    const bar = document.createElement('div');
    bar.innerHTML = '<span id="ptrIcon">⬇</span>&nbsp;<span id="ptrText">下拉重新整理</span>';
    Object.assign(bar.style, {
        position: 'fixed', top: '-60px', left: '0', right: '0',
        height: '52px', display: 'flex', alignItems: 'center',
        justifyContent: 'center', zIndex: '999',
        color: '#3b82f6', fontWeight: 'bold', fontSize: '.95rem',
        pointerEvents: 'none', transition: 'top .15s'
    });
    document.body.appendChild(bar);
    const icon = bar.querySelector('#ptrIcon');
    const text = bar.querySelector('#ptrText');

    let startY = 0, pulling = false, dist = 0;
    const THRESHOLD = 85;

    document.addEventListener('touchstart', e => {
        // 彈窗（詳細頁/放大圖）內滑動不觸發
        if (e.target.closest && e.target.closest('.overlay, .detail-overlay')) {
            pulling = false;
            return;
        }
        if (window.scrollY <= 0) {
            startY = e.touches[0].clientY;
            pulling = true;
            dist = 0;
        } else {
            pulling = false;
        }
    }, {passive: true});

    document.addEventListener('touchmove', e => {
        if (!pulling) return;
        dist = e.touches[0].clientY - startY;
        if (dist > 0 && window.scrollY <= 0) {
            bar.style.transition = 'none';
            bar.style.top = (Math.min(dist / 2, 70) - 52) + 'px';
            const ready = dist > THRESHOLD;
            icon.textContent = ready ? '↻' : '⬇';
            text.textContent = ready ? '放開即重新整理' : '下拉重新整理';
        }
    }, {passive: true});

    document.addEventListener('touchend', () => {
        if (!pulling) return;
        pulling = false;
        bar.style.transition = 'top .15s';
        if (dist > THRESHOLD && window.scrollY <= 0) {
            icon.textContent = '↻';
            text.textContent = '重新整理中…';
            bar.style.top = '10px';
            setTimeout(() => location.reload(), 150);
        } else {
            bar.style.top = '-60px';
        }
        dist = 0;
    }, {passive: true});
})();
