/* 天天生鲜 · 通用前端脚本（无框架依赖） */

/** 取 CSRF token，所有 POST 请求必须带上 */
function getCookie(name) {
    const m = document.cookie.match(new RegExp('(^|;\\s*)' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[2]) : '';
}
const csrfMeta = document.querySelector('meta[name="csrf-token"]');
const CSRF_TOKEN = (csrfMeta && csrfMeta.content) || getCookie('csrftoken');

/** 表单编码 POST，返回 JSON */
async function postJSON(url, data) {
    const body = new URLSearchParams();
    for (const [k, v] of Object.entries(data || {})) body.append(k, v);
    // 登录失效时回到当前页面，而不是跳到 AJAX 接口本身
    const next = new URLSearchParams({_next: window.location.pathname + window.location.search});
    const requestUrl = url + (url.includes('?') ? '&' : '?') + next.toString();
    const resp = await fetch(requestUrl, {
        method: 'POST',
        headers: {'X-CSRFToken': CSRF_TOKEN, 'Content-Type': 'application/x-www-form-urlencoded'},
        body: body.toString(),
    });
    let payload;
    try {
        payload = await resp.json();
    } catch (err) {
        return {code: resp.status || -1, msg: '请求失败，请稍后重试'};
    }
    // AJAX 接口未登录时由后端返回登录地址，前端统一跳转，登录后回到当前页
    if (resp.status === 401 && payload.login_url) {
        window.location.assign(payload.login_url);
    }
    return payload;
}

/** 轻量 toast，避免整页刷新时信息丢失 */
function toast(msg, type) {
    let box = document.getElementById('toast-box');
    if (!box) {
        box = document.createElement('div');
        box.id = 'toast-box';
        box.style.cssText = 'position:fixed;top:20px;left:50%;transform:translateX(-50%);z-index:9999;';
        document.body.appendChild(box);
    }
    const el = document.createElement('div');
    el.className = 'msg msg-' + (type || 'success');
    el.style.cssText = 'box-shadow:0 4px 14px rgba(0,0,0,.15);min-width:200px;text-align:center;';
    el.textContent = msg;
    box.appendChild(el);
    setTimeout(() => el.remove(), 2200);
}

/** 刷新顶部购物车角标 */
function setCartBadge(n) {
    document.querySelectorAll('[data-cart-badge]').forEach(el => {
        el.textContent = n;
        el.style.display = n > 0 ? 'inline-block' : 'none';
    });
}

document.addEventListener('DOMContentLoaded', () => {
    // 首页：轮播图切换（仅当存在多张时启用）
    const heroCarousel = document.querySelector('[data-banner-carousel]');
    if (heroCarousel) {
        const slides = Array.from(heroCarousel.querySelectorAll('.hero-slide'));
        const dots = Array.from(heroCarousel.querySelectorAll('.hero-dot'));
        if (slides.length > 1) {
            let current = 0;
            let timer = null;
            const show = (index) => {
                current = (index + slides.length) % slides.length;
                slides.forEach((slide, i) => {
                    const active = i === current;
                    slide.classList.toggle('active', active);
                    slide.setAttribute('aria-hidden', active ? 'false' : 'true');
                });
                dots.forEach((dot, i) => {
                    const active = i === current;
                    dot.classList.toggle('active', active);
                    dot.setAttribute('aria-current', active ? 'true' : 'false');
                });
            };
            const stop = () => {
                if (timer) {
                    clearInterval(timer);
                    timer = null;
                }
            };
            const start = () => {
                stop();
                if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
                timer = setInterval(() => show(current + 1), 4500);
            };
            dots.forEach((dot, index) => {
                dot.addEventListener('click', () => {
                    show(index);
                    start();
                });
            });
            heroCarousel.addEventListener('mouseenter', stop);
            heroCarousel.addEventListener('mouseleave', start);
            document.addEventListener('visibilitychange', () => document.hidden ? stop() : start());
            show(0);
            start();
        }
    }

    // 详情页：数量加减
    const minus = document.querySelector('[data-qty-minus]');
    const plus = document.querySelector('[data-qty-plus]');
    const qtyInput = document.querySelector('[data-qty-input]');
    // 数量上限取详情页写死的库存，没有该属性时不设限
    const qtyMax = qtyInput ? parseInt(qtyInput.dataset.qtyMax, 10) || Infinity : Infinity;
    const setQty = (value) => {
        if (!qtyInput) return;
        qtyInput.value = Math.max(1, Math.min(qtyMax, value || 1));
    };
    if (minus && plus && qtyInput) {
        minus.onclick = () => setQty((parseInt(qtyInput.value, 10) || 1) - 1);
        plus.onclick = () => setQty((parseInt(qtyInput.value, 10) || 1) + 1);
        qtyInput.addEventListener('change', () => setQty(parseInt(qtyInput.value, 10)));
    }

    // 详情页：加入购物车
    const addBtn = document.querySelector('[data-add-cart]');
    if (addBtn) {
        addBtn.onclick = async () => {
            if (addBtn.disabled) return;
            addBtn.disabled = true;  // 请求期间禁用，避免连点累加两次
            try {
                const res = await postJSON(addBtn.dataset.addCart, {count: qtyInput ? qtyInput.value : 1});
                if (res.code === 0) {
                    toast(res.msg || '已加入购物车');
                    if (res.cart_count !== undefined) setCartBadge(res.cart_count);
                } else if (res.code !== 401) {
                    toast(res.msg || '操作失败', 'error');
                    // 超过库存时把数量回填到上限，用户改一下就能直接加购
                    if (res.code === 'stock_limit' && qtyMax !== Infinity) setQty(qtyMax);
                }
            } finally {
                addBtn.disabled = false;
            }
        };
    }

    // 购物车页：数量增减与删除，按行局部刷新，不整页 reload
    const cartRow = (btn) => btn.closest('[data-cart-row]');

    /** 把行内按钮的“预期数量”和“减号可用性”同步到当前数量 */
    const syncQtyRow = (row) => {
        if (!row) return;
        const input = row.querySelector('[data-qty-input]');
        const current = input ? parseInt(input.value, 10) || 1 : 1;
        row.querySelectorAll('[data-cart-update]').forEach(btn => {
            btn.dataset.expected = current;
            if (parseInt(btn.dataset.step, 10) < 0) btn.disabled = current <= 1;
        });
    };

    /** 用接口返回的数量/金额更新行、底部合计和顶部角标 */
    const applyCartResult = (row, res) => {
        if (row && (res.removed || res.count === 0)) {
            row.remove();
        } else if (row) {
            const input = row.querySelector('[data-qty-input]');
            if (input) input.value = res.count;
            const subtotal = row.querySelector('[data-cart-subtotal]');
            if (subtotal) subtotal.textContent = '¥' + res.amount;
        }
        const countEl = document.querySelector('[data-cart-total-count]');
        const amountEl = document.querySelector('[data-cart-total-amount]');
        const payEl = document.querySelector('[data-cart-total-pay]');
        const shippingEl = document.querySelector('[data-cart-shipping-fee]');
        if (countEl) countEl.textContent = res.total_count;
        if (amountEl) amountEl.textContent = res.total_amount;
        if (payEl && res.total_pay !== undefined) payEl.textContent = res.total_pay;
        if (shippingEl && res.shipping_fee !== undefined) shippingEl.textContent = res.shipping_fee;
        if (res.cart_count !== undefined) setCartBadge(res.cart_count);
    };

    const submitCartRequest = async (btn, url, data) => {
        const row = cartRow(btn);
        const group = row ? Array.from(row.querySelectorAll('button')) : [btn];
        group.forEach(item => item.disabled = true);  // 防连点
        try {
            const res = await postJSON(url, data || {});
            if (res.code === 0) {
                applyCartResult(row, res);
                if (res.total_count === 0) {
                    location.reload();  // 购物车已空，交给模板渲染空态
                }
            } else if (res.code !== 401) {
                toast(res.msg || '操作失败', 'error');
                // 其他页面改过同一商品，回到服务端的最新状态
                if (res.code === 'cart_conflict') setTimeout(() => location.reload(), 800);
            }
        } finally {
            group.forEach(item => item.disabled = false);
            syncQtyRow(row);
        }
    };

    document.querySelectorAll('[data-cart-update]').forEach(btn => {
        btn.onclick = () => {
            const row = cartRow(btn);
            const input = row && row.querySelector('[data-qty-input]');
            const current = input ? parseInt(input.value, 10) || 1 : 1;
            const next = Math.max(1, current + (parseInt(btn.dataset.step, 10) || 0));
            const maxEl = btn.closest('[data-qty-max]');
            const max = maxEl ? parseInt(maxEl.dataset.qtyMax, 10) : NaN;
            if (next > max) {
                toast('库存仅剩 ' + max + ' 件', 'error');
                return;
            }
            submitCartRequest(btn, btn.dataset.cartUpdate, {count: next, expected: current});
        };
    });

    document.querySelectorAll('[data-cart-delete]').forEach(btn => {
        btn.onclick = () => {
            const row = cartRow(btn);
            const input = row && row.querySelector('[data-qty-input]');
            const current = input ? parseInt(input.value, 10) || 1 : btn.dataset.expected;
            submitCartRequest(btn, btn.dataset.cartDelete, {expected: current});
        };
    });

    // 确认订单页：选择收货地址
    const addrInput = document.querySelector('input[name="addr_id"]');
    document.querySelectorAll('.addr-item').forEach(item => {
        item.onclick = () => {
            document.querySelectorAll('.addr-item').forEach(i => i.classList.remove('active'));
            item.classList.add('active');
            if (addrInput) addrInput.value = item.dataset.addrId;
        };
    });

    // 确认订单页：提交后立刻禁用按钮，双击只发一次（服务端还有一次性令牌兜底）
    const orderForm = document.getElementById('order-form');
    if (orderForm) {
        orderForm.addEventListener('submit', () => {
            const submitBtn = orderForm.querySelector('button[type="submit"]');
            if (submitBtn) {
                submitBtn.disabled = true;
                submitBtn.textContent = '提交中…';
            }
        });
    }
});
