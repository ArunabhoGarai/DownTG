/**
 * Automated VNC MiniApp Token Capture Engine
 * ===========================================
 * Automates Puppeteer Extra Stealth inside Telegram Web on DISPLAY=:99:
 * 1. Navigates to target bot chat in Telegram Web.
 * 2. Clicks START if needed, then locates and triggers the MiniApp button.
 * 3. Handles Telegram Web's launch confirmation modal.
 * 4. Navigates into the MiniApp iframe.
 * 5. Types a dummy link into the link input with human-like delays.
 * 6. Clicks the Download button to trigger backend API authentication.
 * 7. Intercepts and extracts the Bearer Authorization token.
 * 
 * Usage:
 *   node vnc_auto_capture.js <platform: diskwala|tera> <target_url> [user_data_dir]
 */

const path = require('path');
const fs = require('fs');

const PLATFORM = (process.argv[2] || 'diskwala').toLowerCase();

// Direct Telegram Web A peer URLs
const DEFAULT_URLS = {
    diskwala: 'https://web.telegram.org/a/#7802633228',
    tera: 'https://web.telegram.org/a/#7802009139',
    terabox: 'https://web.telegram.org/a/#7802009139',
};

const TARGET_URL = process.argv[3] || DEFAULT_URLS[PLATFORM] || DEFAULT_URLS['diskwala'];
const USER_DATA_DIR = process.argv[4] || path.join(__dirname, 'data', 'tg_browser_profile');

// Dummy links designed to trigger the backend API without errors
const DUMMY_LINKS = {
    diskwala: 'https://www.diskwala.com/app/6a992e7206ba7ea03daf9137',
    tera: 'https://www.terabox.app/sharing/link?surl=shl4DnwTd2xTki0tHnAOyQ',
    terabox: 'https://www.terabox.app/sharing/link?surl=shl4DnwTd2xTki0tHnAOyQ',
};

if (!fs.existsSync(USER_DATA_DIR)) {
    fs.mkdirSync(USER_DATA_DIR, { recursive: true });
}

// Clean up stale lock/port files if any
const devtoolsPortFile = path.join(USER_DATA_DIR, 'DevToolsActivePort');
if (fs.existsSync(devtoolsPortFile)) {
    try { fs.unlinkSync(devtoolsPortFile); } catch (_) {}
}

function logStatus(msg) {
    console.log(`[AUTOVNC_STATUS] ${msg}`);
}

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

function saveTokenToEnv(key, token) {
    try {
        const envPath = path.join(__dirname, '.env');
        if (!fs.existsSync(envPath)) return;
        const cleanVal = token.startsWith('Bearer ') ? token : `Bearer ${token.trim()}`;
        let content = fs.readFileSync(envPath, 'utf8');
        const regex = new RegExp(`^${key}=.*$`, 'm');
        if (regex.test(content)) {
            content = content.replace(regex, `${key}=${cleanVal}`);
        } else {
            content += `\n${key}=${cleanVal}\n`;
        }
        fs.writeFileSync(envPath, content, 'utf8');
        logStatus(`💾 Token persisted to .env (${key})`);
    } catch (e) {
        logStatus(`Notice: .env write skipped: ${e.message}`);
    }
}

let currentMousePos = { x: 350, y: 350 };

// Exact viewport coordinates provided by user (calibrated for standard 1920x1080 display):
const DISKWALA_COORDS = {
    openButton: { x: 794, y: 1008 },
    linkInput: { x: 987, y: 402 },
    downloadBtn: { x: 1154, y: 399 },
};

/**
 * Generates a smooth, curved Bézier trajectory with human-like acceleration and micro-jitter.
 */
async function humanMouseMove(page, targetX, targetY) {
    const startX = currentMousePos.x;
    const startY = currentMousePos.y;
    const dx = targetX - startX;
    const dy = targetY - startY;
    const distance = Math.hypot(dx, dy);

    if (distance < 5) {
        await page.mouse.move(targetX, targetY);
        currentMousePos = { x: targetX, y: targetY };
        return;
    }

    const steps = Math.max(15, Math.min(35, Math.floor(distance / 20)));
    const midX = (startX + targetX) / 2;
    const midY = (startY + targetY) / 2;
    const perpX = -dy / distance;
    const perpY = dx / distance;
    const curveOffset = (Math.random() - 0.5) * Math.min(distance * 0.3, 100);
    const ctrlX = midX + perpX * curveOffset;
    const ctrlY = midY + perpY * curveOffset;

    for (let i = 1; i <= steps; i++) {
        const t = i / steps;
        const ease = t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t;
        const u = 1 - ease;
        const x = u * u * startX + 2 * u * ease * ctrlX + ease * ease * targetX;
        const y = u * u * startY + 2 * u * ease * ctrlY + ease * ease * targetY;

        const jitterX = (Math.random() - 0.5) * 1.2;
        const jitterY = (Math.random() - 0.5) * 1.2;

        await page.mouse.move(x + jitterX, y + jitterY);
        await sleep(Math.floor(Math.random() * 8) + 6);
    }

    await page.mouse.move(targetX, targetY);
    currentMousePos = { x: targetX, y: targetY };
}

/**
 * Calculates absolute viewport coordinates for an element (including elements inside nested iframes).
 */
async function getElementViewportCoords(page, elementHandle, frame = null) {
    if (!elementHandle) return null;

    try {
        let box = await elementHandle.boundingBox();
        if (box && box.width > 0 && box.height > 0) {
            return {
                x: box.x,
                y: box.y,
                width: box.width,
                height: box.height,
            };
        }

        if (frame && frame !== page.mainFrame()) {
            let frameBox = null;
            try {
                const frameEl = await frame.frameElement();
                if (frameEl) {
                    frameBox = await frameEl.boundingBox();
                }
            } catch (_) {}

            const relRect = await frame.evaluate(el => {
                const r = el.getBoundingClientRect();
                return { x: r.left, y: r.top, width: r.width, height: r.height };
            }, elementHandle);

            if (relRect && relRect.width > 0 && relRect.height > 0) {
                const offsetX = frameBox ? frameBox.x : 0;
                const offsetY = frameBox ? frameBox.y : 0;
                return {
                    x: offsetX + relRect.x,
                    y: offsetY + relRect.y,
                    width: relRect.width,
                    height: relRect.height,
                };
            }
        }
    } catch (_) {}

    return null;
}

/**
 * Performs a true hardware-level CDP mouse click with realistic motion kinematics.
 */
async function humanClick(page, elementHandle, frame = null, label = 'element') {
    if (!elementHandle) return false;

    try {
        const targetContext = frame || page;
        await targetContext.evaluate(el => {
            if (el.classList.contains('hide')) {
                el.classList.remove('hide');
                el.style.display = 'inline-flex';
                el.style.visibility = 'visible';
                el.style.pointerEvents = 'auto';
                el.style.opacity = '1';
            }
            el.scrollIntoView({ behavior: 'instant', block: 'center', inline: 'center' });
        }, elementHandle);

        await sleep(250);

        const coords = await getElementViewportCoords(page, elementHandle, frame);

        if (coords && coords.width > 0 && coords.height > 0) {
            const targetX = coords.x + coords.width * (0.2 + Math.random() * 0.6);
            const targetY = coords.y + coords.height * (0.2 + Math.random() * 0.6);

            await humanMouseMove(page, targetX, targetY);
            await sleep(Math.floor(Math.random() * 80) + 60);

            await page.mouse.down({ button: 'left' });
            await sleep(Math.floor(Math.random() * 50) + 70);
            await page.mouse.up({ button: 'left' });

            logStatus(`🖱️ Real mouse clicked on ${label} at (${Math.round(targetX)}, ${Math.round(targetY)})`);
            return true;
        } else {
            logStatus(`⚠️ Bounding box not resolved for ${label}, falling back to handle.click()...`);
            await elementHandle.click({ delay: Math.floor(Math.random() * 50) + 70 });
            return true;
        }
    } catch (err) {
        logStatus(`humanClick warning for ${label}: ${err.message}. Trying direct handle click...`);
        try {
            await elementHandle.click({ delay: 80 });
            return true;
        } catch (_) {}
    }
    return false;
}

/**
 * Performs human typing with realistic mouse focus, key clear, and variable keystroke delays.
 */
async function humanType(page, inputHandle, text, frame = null) {
    if (!inputHandle) return false;

    await humanClick(page, inputHandle, frame, 'input field');
    await sleep(400);

    await page.keyboard.down('Control');
    await page.keyboard.press('KeyA');
    await page.keyboard.up('Control');
    await sleep(100);
    await page.keyboard.press('Backspace');
    await sleep(200);

    for (const char of text) {
        await page.keyboard.type(char, { delay: Math.floor(Math.random() * 45) + 50 });
    }
    await sleep(400);

    const targetContext = frame || page;
    const value = await targetContext.evaluate(el => el.value, inputHandle);
    if (value !== text) {
        logStatus(`Notice: input value had discrepancy ("${value}"). Correcting...`);
        await targetContext.evaluate((el, val) => {
            el.value = val;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }, inputHandle, text);
    }

    return true;
}

/**
 * Resolves coordinates adapting for viewport resolution differences if any.
 * On 1920x1080 (standard EC2 VNC), this returns the exact input coordinates.
 */
async function resolveAdaptiveCoords(page, baseCoords) {
    try {
        const dims = await page.evaluate(() => ({
            width: window.innerWidth,
            height: window.innerHeight,
        }));
        // If within standard 1920x1080 bounds, use exact coordinates (clamped to viewport)
        if (dims.width >= 1800 && dims.height >= 950) {
            const clampedY = Math.min(baseCoords.y, dims.height - 10);
            return { x: baseCoords.x, y: clampedY };
        }
        // Scaled for smaller test viewports (e.g. 1280x577 local display)
        const scaleX = dims.width / 1920;
        const scaleY = dims.height / 1080;
        return {
            x: Math.round(baseCoords.x * scaleX),
            y: Math.round(baseCoords.y * scaleY),
        };
    } catch (_) {
        return baseCoords;
    }
}

/**
 * Performs a true hardware-level CDP mouse click at specific coordinates (x, y)
 * using realistic Bézier motion, micro-jitter, and authentic down/up hold times.
 */
async function humanClickCoords(page, targetX, targetY, label = 'coordinate') {
    logStatus(`🎯 Moving real mouse along Bézier curve to (${Math.round(targetX)}, ${Math.round(targetY)}) for ${label}...`);
    await humanMouseMove(page, targetX, targetY);
    await sleep(Math.floor(Math.random() * 60) + 70); // Hover reaction pause

    await page.mouse.down({ button: 'left' });
    await sleep(Math.floor(Math.random() * 40) + 70); // Human click hold time (70-110ms)
    await page.mouse.up({ button: 'left' });

    logStatus(`🖱️ Real mouse clicked at (${Math.round(targetX)}, ${Math.round(targetY)}) [${label}]`);
    return true;
}

/**
 * Robustly isolates the MiniApp Download/Submit button inside the iframe DOM.
 */
async function findDownloadButton(frame) {
    if (!frame) return null;
    try {
        const btnHandles = await frame.$$('button, div[role="button"], a[role="button"], .btn');
        for (const bh of btnHandles) {
            const isDl = await frame.evaluate((b) => {
                const t = (b.innerText || b.textContent || '').toLowerCase().trim();
                const r = b.getBoundingClientRect();
                return (
                    (t.includes('download') || t.includes('get') || t.includes('submit') || t.includes('fetch') || t.includes('play')) &&
                    r.width > 0 && r.height > 0
                );
            }, bh);
            if (isDl) return bh;
        }

        const siblingHandle = await frame.evaluateHandle(() => {
            const input = document.querySelector('input');
            if (input && input.parentElement) {
                return input.parentElement.querySelector('button') || input.closest('form, div').querySelector('button');
            }
            return document.querySelector('button');
        });
        if (siblingHandle && siblingHandle.asElement()) {
            return siblingHandle.asElement();
        }
    } catch (_) {}
    return null;
}

// 360-second (6 minute) watchdog timer to handle slow EC2 VNC environments
const HARD_TIMEOUT_MS = 360 * 1000;
let browser = null;
let isExiting = false;

const cleanupAndExit = async (code, resultType, message) => {
    if (isExiting) return;
    isExiting = true;
    try {
        if (browser) {
            logStatus('Closing browser session cleanly...');
            await browser.close();
            browser = null;
        }
    } catch (_) {}
    if (resultType && message) {
        console.log(`[${resultType}] ${message}`);
    }
    process.exit(code);
};

const watchdog = setTimeout(async () => {
    logStatus('Operation timed out (exceeded 6 minutes). Aborting...');
    await cleanupAndExit(1, 'AUTOVNC_ERROR', 'Auto-VNC session timed out on EC2.');
}, HARD_TIMEOUT_MS);
if (watchdog.unref) watchdog.unref();

['SIGINT', 'SIGTERM', 'SIGHUP'].forEach(sig => {
    process.on(sig, async () => {
        logStatus(`Received ${sig}. Shutting down browser...`);
        await cleanupAndExit(1, 'AUTOVNC_ERROR', `Interrupted by ${sig}`);
    });
});

(async () => {
    try {
        let puppeteer;
        try {
            const { addExtra } = require('puppeteer-extra');
            const StealthPlugin = require('puppeteer-extra-plugin-stealth');
            const puppeteerVanillaModule = await import('puppeteer');
            const puppeteerVanilla = puppeteerVanillaModule.default || puppeteerVanillaModule;

            puppeteer = addExtra(puppeteerVanilla);
            puppeteer.use(StealthPlugin());
        } catch (err) {
            try {
                const p = await import('puppeteer');
                puppeteer = p.default || p;
            } catch (e) {
                console.log(`[AUTOVNC_ERROR] Puppeteer is not installed: ${e.message}`);
                process.exit(1);
            }
        }

        logStatus('Launching Chrome GUI with persistent profile...');
        browser = await puppeteer.launch({
            headless: false, // GUI mode on Xvfb/Desktop
            userDataDir: USER_DATA_DIR,
            defaultViewport: null,
            args: [
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--start-maximized',
                '--window-position=0,0',
                '--window-size=1920,1080',
                '--disable-dev-shm-usage',
                '--disable-infobars',
                '--no-first-run',
                '--no-default-browser-check',
            ],
            ignoreDefaultArgs: ['--enable-automation'],
        });

        await sleep(800);

        const initialPages = await browser.pages();
        let page = initialPages[0];

        // Close any other tabs (e.g. blank tabs, newtab, about:blank) so only 1 tab exists
        for (let i = 1; i < initialPages.length; i++) {
            try {
                await initialPages[i].close();
            } catch (_) {}
        }

        // DO NOT force setViewport(1920, 1080) because fixed 1080 height on screens
        // with browser UI / taskbars causes the bottom of Telegram (input/buttons) to be cut off!
        await page.bringToFront();

        await page.evaluateOnNewDocument(() => {
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
            });
            if (!window.chrome) {
                window.chrome = { runtime: {} };
            }
        });

        try {
            const dims = await page.evaluate(() => {
                return {
                    innerWidth: window.innerWidth,
                    innerHeight: window.innerHeight,
                    availHeight: window.screen.availHeight || window.screen.height,
                };
            });
            logStatus(`Detected viewport area: ${dims.innerWidth}x${dims.innerHeight} (Screen available: ${dims.availHeight}h)`);

            if (dims.innerHeight < 820) {
                logStatus('Compact vertical space detected (<820px). Applying 90% zoom so bottom controls & MiniApp are fully visible...');
                await page.evaluate(() => {
                    document.documentElement.style.zoom = '90%';
                });
            }
        } catch (_) {}

        let tokenCaptured = false;

        const isTera = (PLATFORM === 'tera' || PLATFORM === 'terabox');
        const targetEnvKey = isTera ? 'TERABOX_BEARER_TOKEN' : 'DISKWALA_BEARER_TOKEN';
        const targetTag = isTera ? '[TERABOX_TOKEN_CAPTURED]' : '[DISKWALA_TOKEN_CAPTURED]';
        const targetName = isTera ? 'TeraBox' : 'Diskwala';

        // 1. Setup Request Interception across all frames and pages
        async function setupInterception(targetPage) {
            try {
                targetPage.on('request', async (req) => {
                    const url = req.url();
                    const headers = req.headers();
                    const auth = headers['authorization'] || headers['Authorization'];

                    if (!auth) return;
                    if (!auth.startsWith('Bearer ') && !auth.includes('query_id=') && !auth.includes('user=')) return;

                    // Match any API request originating from the MiniApp
                    const isApiRequest = (
                        url.includes('diskwala') ||
                        url.includes('api2.diskwala.net') ||
                        url.includes('miniapp.diskwala.net') ||
                        url.includes('terabox') ||
                        url.includes('teradownloader') ||
                        url.includes('apiwala') ||
                        url.includes('/api/') ||
                        url.includes('/download')
                    );

                    if (isApiRequest) {
                        if (!tokenCaptured) {
                            tokenCaptured = true;
                            console.log(`${targetTag} ${auth}`);
                            console.log(`[TOKEN_CAPTURED] ${auth}`);
                            logStatus(`🎉 ${targetName} token captured successfully!`);
                            saveTokenToEnv(targetEnvKey, auth);
                        }
                    }
                });
            } catch (_) {}
        }

        function isSignedMiniAppUrl(u, plat) {
            if (!u || typeof u !== 'string') return false;
            if (!u.includes('tgWebAppData=')) return false;
            const lower = u.toLowerCase();
            if (plat === 'diskwala') {
                return lower.includes('diskwala') || lower.includes('miniapp') || !lower.includes('teradownloader');
            } else {
                // For TeraBox: accept any signed MiniApp URL that isn't diskwala
                return lower.includes('teradownloader') || lower.includes('twa.') || lower.includes('terabox') || lower.includes('apiwala') || !lower.includes('diskwala');
            }
        }

        let miniappUrl = null;

        page.on('framenavigated', (frame) => {
            try {
                const u = frame.url();
                if (isSignedMiniAppUrl(u, PLATFORM) && !miniappUrl) {
                    logStatus(`🎯 Captured signed MiniApp URL from frame navigation: ${u.slice(0, 80)}...`);
                    miniappUrl = u;
                }
            } catch (_) {}
        });

        page.on('request', (req) => {
            try {
                const u = req.url();
                if (isSignedMiniAppUrl(u, PLATFORM) && !miniappUrl) {
                    logStatus(`🎯 Captured signed MiniApp URL from network request: ${u.slice(0, 80)}...`);
                    miniappUrl = u;
                }
            } catch (_) {}
        });

        await setupInterception(page);

        const targetHash = TARGET_URL.includes('#') ? ('#' + TARGET_URL.split('#')[1]) : '';

        // Secondary tab tracker: if Chrome opens the target link in a secondary tab, switch to it!
        browser.on('targetcreated', async (target) => {
            if (target.type() === 'page') {
                try {
                    const newPage = await target.page();
                    if (newPage && newPage !== page) {
                        const u = newPage.url();
                        if (isSignedMiniAppUrl(u, PLATFORM) && !miniappUrl) {
                            logStatus(`🎯 Captured signed MiniApp URL from spawned tab: ${u.slice(0, 80)}...`);
                            miniappUrl = u;
                        }
                        newPage.on('framenavigated', (frame) => {
                            try {
                                const fu = frame.url();
                                if (isSignedMiniAppUrl(fu, PLATFORM) && !miniappUrl) {
                                    logStatus(`🎯 Captured signed MiniApp URL from spawned tab frame: ${fu.slice(0, 80)}...`);
                                    miniappUrl = fu;
                                }
                            } catch (_) {}
                        });
                        await setupInterception(newPage);
                        if (targetHash && u.includes(targetHash)) {
                            logStatus('Target chat opened in secondary tab. Bringing it to front!');
                            await newPage.bringToFront();
                            try { await page.close(); } catch (_) {}
                            page = newPage;
                        }
                    }
                } catch (_) {}
            }
        });

        // 2. Navigate directly to TARGET_URL on page
        logStatus(`Navigating directly to target chat: ${TARGET_URL}...`);
        try {
            await page.goto(TARGET_URL, { waitUntil: 'domcontentloaded', timeout: 90000 });
        } catch (e) {
            logStatus(`Navigation notice: ${e.message}. Continuing...`);
        }

        // Force hash update in SPA router if needed
        if (targetHash) {
            await sleep(2500);
            await page.evaluate((h) => {
                if (window.location.hash !== h) {
                    window.location.hash = h;
                }
            }, targetHash);
        }
        await page.bringToFront();

        // 3. Check for login state (QR code or phone entry)
        logStatus('Checking Telegram Web session state...');
        await sleep(5000);

        let isLoginPage = false;
        // Verify login state with 3 checks over 6 seconds so we don't trip during initial page load on slow EC2
        for (let check = 0; check < 3; check++) {
            isLoginPage = await page.evaluate(() => {
                const hasQr = document.querySelector('.qr-code, canvas, .qr-container, .auth-qr');
                const hasPhoneInput = document.querySelector('input[type="tel"], .input-field-input, #sign-in-phone-number');
                const hasLoginTitle = Array.from(document.querySelectorAll('h1, h2, h3, h4, div')).some(el => {
                    const t = (el.innerText || '').toLowerCase();
                    return t.includes('log in to telegram') || t.includes('scan from mobile');
                });
                return Boolean((hasQr || hasPhoneInput) && hasLoginTitle);
            });
            if (!isLoginPage) break;
            await sleep(2000);
        }

        if (isLoginPage) {
            console.log('[AUTOVNC_LOGIN_REQUIRED] Telegram Web is not logged in! Please run /vnc to log in.');
            await cleanupAndExit(1, 'AUTOVNC_ERROR', 'Telegram Web session is logged out. Login required via /vnc.');
            return;
        }

        // 4. Wait for Telegram Web chat interface to load (allowing up to 120s on slow EC2)
        logStatus('Waiting for chat interface to render (giving up to 2 minutes for slow EC2)...');
        let chatLoaded = false;
        for (let i = 0; i < 60; i++) {
            chatLoaded = await page.evaluate(() => {
                return Boolean(
                    document.querySelector('.chat, .messages-container, .bubbles, .middle-column, .chat-input, .MessageList')
                );
            });
            if (chatLoaded) break;

            if (i > 0 && i % 5 === 0) {
                logStatus(`Still loading Telegram Web interface... (${i * 2}s elapsed)`);
                if (targetHash && i === 20) {
                    // Try nudging router hash if stalled
                    await page.evaluate((h) => { window.location.hash = h; }, targetHash);
                }
            }
            await sleep(2000);
        }

        if (!chatLoaded) {
            logStatus('Chat container did not finish rendering within 120s. Attempting to proceed with available DOM...');
            if (targetHash) {
                await page.evaluate((h) => { window.location.hash = h; }, targetHash);
            }
        } else {
            logStatus('✅ Chat interface loaded successfully!');
        }

        await sleep(2000);

        // Ensure message history is scrolled down
        try {
            await page.evaluate(() => {
                const bubbles = document.querySelector('.middle-column, .messages-container, .bubbles, .MessageList');
                if (bubbles) bubbles.scrollTop = bubbles.scrollHeight;
            });
        } catch (_) {}

        // Reusable helper to detect and confirm Telegram Web launch popups ("Open this web app?", "Confirm", "Launch")
        // NOTE: Uses non-intrusive DOM click (no mouse movement or random mouse clicks)
        async function handleLaunchConfirmationModal(p) {
            try {
                const confirmedText = await p.evaluate(() => {
                    const modal = document.querySelector('.popup, .modal-dialog, .confirm-dialog, .popup-container, .modal, .Modal, div[role="dialog"], .Dialog, .ConfirmDialog');
                    if (!modal) return null;
                    const buttons = Array.from(modal.querySelectorAll('button, .btn, .popup-button, .confirm-dialog-button, .Button, div[role="button"]'));
                    for (const btn of buttons) {
                        const text = (btn.innerText || btn.textContent || '').trim().toLowerCase();
                        const rect = btn.getBoundingClientRect();
                        const isVisible = rect.width > 0 && rect.height > 0 && window.getComputedStyle(btn).visibility !== 'hidden';
                        if (
                            isVisible &&
                            (text === 'confirm' || text.includes('confirm') || text === 'launch' || text.includes('launch') || text === 'proceed' || text.includes('proceed') || text === 'open' || text.includes('open') || text === 'ok') &&
                            !text.includes('cancel') &&
                            !text.includes('close')
                        ) {
                            btn.click();
                            return (btn.innerText || btn.textContent || '').trim();
                        }
                    }
                    return null;
                });

                if (confirmedText) {
                    logStatus(`✅ Confirmed launch popup via DOM click: "${confirmedText}"`);
                    return true;
                }
            } catch (_) {}
            return false;
        }

        // 5. Inject MutationObserver into Telegram Web DOM to instantly catch signed MiniApp iframe insertion
        try {
            await page.evaluate(() => {
                window.__capturedSignedMiniAppUrl = null;
                const observer = new MutationObserver((mutations) => {
                    for (const m of mutations) {
                        for (const node of m.addedNodes) {
                            if (node.nodeType === 1) {
                                if (node.tagName === 'IFRAME') {
                                    const src = node.src || node.getAttribute('src') || '';
                                    if (src.includes('tgWebAppData=')) {
                                        window.__capturedSignedMiniAppUrl = src;
                                    }
                                }
                                if (node.querySelectorAll) {
                                    const iframes = node.querySelectorAll('iframe');
                                    for (const ifr of iframes) {
                                        const src = ifr.src || ifr.getAttribute('src') || '';
                                        if (src.includes('tgWebAppData=')) {
                                            window.__capturedSignedMiniAppUrl = src;
                                        }
                                    }
                                }
                            }
                        }
                    }
                });
                observer.observe(document.body || document.documentElement, { childList: true, subtree: true });
            });
        } catch (_) {}

        // Execute the ONLY automated mouse click: the Open button at coordinates (X: 794, Y: 1008)
        logStatus(`Executing the single hardware click on Open button at coordinates (X: 794, Y: 1008)...`);
        try {
            const openCoords = await resolveAdaptiveCoords(page, DISKWALA_COORDS.openButton);
            await humanClickCoords(page, openCoords.x, openCoords.y, 'Open button');
            logStatus('🛑 First click executed. All further automated mouse clicks stopped.');
        } catch (e) {
            logStatus(`Open button click notice: ${e.message}`);
        }

        await sleep(2000);
        await handleLaunchConfirmationModal(page);

        // 8. Locate and isolate the SIGNED MiniApp iframe URL (must contain tgWebAppData=)
        logStatus(`Locating and isolating signed ${targetName} MiniApp URL with tgWebAppData (waiting up to 90s for slow EC2)...`);
        let targetFrame = null;
        const frameWaitStart = Date.now();

        while (!miniappUrl && (Date.now() - frameWaitStart) < 90000) {
            if (tokenCaptured) break;

            // Continuously check for and click the confirmation popup if it appears delayed
            await handleLaunchConfirmationModal(page);

            // Check MutationObserver immediate capture
            try {
                const obsUrl = await page.evaluate(() => window.__capturedSignedMiniAppUrl);
                if (obsUrl && isSignedMiniAppUrl(obsUrl, PLATFORM)) {
                    logStatus(`🎯 Captured signed MiniApp URL from MutationObserver: ${obsUrl.slice(0, 80)}...`);
                    miniappUrl = obsUrl;
                    break;
                }
            } catch (_) {}

            // Check active frames
            const frames = page.frames();
            for (const f of frames) {
                try {
                    const u = f.url();
                    if (isSignedMiniAppUrl(u, PLATFORM)) {
                        miniappUrl = u;
                        targetFrame = f;
                        break;
                    }
                    const href = await f.evaluate(() => window.location.href);
                    if (isSignedMiniAppUrl(href, PLATFORM)) {
                        miniappUrl = href;
                        targetFrame = f;
                        break;
                    }
                } catch (_) {}
            }

            if (miniappUrl) break;

            // Also check iframe src directly in DOM (including shadow roots and links)
            try {
                const domSignedUrl = await page.evaluate((plat) => {
                    function getIframes(root) {
                        let list = Array.from(root.querySelectorAll('iframe'));
                        try {
                            const allEls = root.querySelectorAll('*');
                            for (const el of allEls) {
                                if (el.shadowRoot) {
                                    list = list.concat(getIframes(el.shadowRoot));
                                }
                            }
                        } catch (_) {}
                        return list;
                    }

                    const iframes = getIframes(document);
                    for (const iframe of iframes) {
                        const src = iframe.src || iframe.getAttribute('src') || iframe.dataset.src || '';
                        if (src && src.includes('tgWebAppData=')) {
                            const lower = src.toLowerCase();
                            const isMatch = (plat === 'diskwala')
                                ? (lower.includes('diskwala') || lower.includes('miniapp') || !lower.includes('teradownloader'))
                                : (lower.includes('teradownloader') || lower.includes('twa.') || lower.includes('terabox') || lower.includes('apiwala') || !lower.includes('diskwala'));
                            if (isMatch) return src;
                        }
                    }

                    // Check links or buttons with signed URL
                    const links = Array.from(document.querySelectorAll('a[href*="tgWebAppData="]'));
                    for (const a of links) {
                        const h = a.href || a.getAttribute('href') || '';
                        if (h && h.includes('tgWebAppData=')) return h;
                    }

                    return null;
                }, PLATFORM);

                if (domSignedUrl) {
                    miniappUrl = domSignedUrl;
                    break;
                }
            } catch (_) {}

            // Fallback: If 8 seconds elapsed after first click and no modal/signed URL appeared,
            // trigger the WebApp button via clean non-intrusive DOM click
            const elapsedSec = Math.round((Date.now() - frameWaitStart) / 1000);
            if (elapsedSec >= 8 && elapsedSec % 8 === 0 && !miniappUrl) {
                try {
                    await page.evaluate((plat) => {
                        const candidateButtons = Array.from(document.querySelectorAll(
                            '.chat-input.chat-input-main button, .chat-input-control-button, .chat-input-plate-button, .bot-menu-button, .reply-markup button, button.is-web-app'
                        ));
                        for (const btn of candidateButtons) {
                            const t = (btn.innerText || btn.textContent || '').toLowerCase();
                            if (
                                btn.classList.contains('is-web-app') || t.includes('open') || t.includes('app') ||
                                t.includes('download') || t.includes(plat) || btn.classList.contains('bot-menu-button')
                            ) {
                                btn.click();
                                break;
                            }
                        }
                    }, PLATFORM);
                } catch (_) {}
            }

            if (elapsedSec > 0 && elapsedSec % 15 === 0) {
                logStatus(`Waiting for signed MiniApp URL with tgWebAppData= (${elapsedSec}s elapsed)...`);
            }

            await sleep(1500);
        }

        // 9. Navigate directly to isolated MiniApp URL and fill the download form
        const dummyUrl = DUMMY_LINKS[PLATFORM] || DUMMY_LINKS['diskwala'];
        let formSubmitted = false;

        if (miniappUrl) {
            logStatus(`🎯 Isolated MiniApp URL: ${miniappUrl.slice(0, 80)}...`);
            logStatus('Navigating directly to isolated MiniApp URL...');
            try {
                await page.goto(miniappUrl, { waitUntil: 'domcontentloaded', timeout: 45000 });
            } catch (navErr) {
                logStatus(`Direct navigation notice: ${navErr.message}. Continuing...`);
            }
            await sleep(3000);

            // Locate the input field on the isolated MiniApp page
            logStatus('Searching for download form input on isolated MiniApp page...');
            let inputHandle = null;
            for (let waitAttempt = 0; waitAttempt < 20; waitAttempt++) {
                if (tokenCaptured) break;
                try {
                    inputHandle = await page.$(
                        'input[placeholder*="Enter Diskwala Link" i], input[placeholder*="Diskwala" i], input[placeholder*="TeraBox" i], input[placeholder*="terabox" i], input[placeholder*="link" i], input[placeholder*="url" i], form.flex input[type="text"], form input[type="text"], form input[type="url"], input[type="text"], input[type="url"], input'
                    );
                    if (inputHandle) {
                        const isVis = await page.evaluate(el => {
                            const r = el.getBoundingClientRect();
                            return r.width > 0 && r.height > 0;
                        }, inputHandle);
                        if (isVis) break;
                    }
                } catch (_) {}
                await sleep(1000);
            }

            if (inputHandle) {
                logStatus('Found download link form input. Focusing and entering dummy link...');
                await inputHandle.focus();
                await page.evaluate(el => el.focus(), inputHandle);
                await sleep(350);

                // Clear input via CDP keyboard
                await page.keyboard.down('Control');
                await page.keyboard.press('KeyA');
                await page.keyboard.up('Control');
                await sleep(100);
                await page.keyboard.press('Backspace');
                await sleep(150);

                logStatus(`Entering dummy link via realistic keystrokes: ${dummyUrl}...`);
                for (const char of dummyUrl) {
                    await page.keyboard.type(char, { delay: Math.floor(Math.random() * 35) + 40 });
                }
                await sleep(400);

                // Ensure value is set in DOM
                const val = await page.evaluate(el => el.value, inputHandle);
                if (!val || val !== dummyUrl) {
                    logStatus('Syncing dummy link value into DOM as safety...');
                    await page.evaluate((el, v) => {
                        el.value = v;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }, inputHandle, dummyUrl);
                }
                await sleep(300);

                // Hit Enter to submit form
                logStatus('Submitting form: hitting ENTER...');
                await page.keyboard.press('Enter');
                formSubmitted = true;
                await sleep(1000);

                // Also click Download submit button via direct DOM click (no mouse movement)
                try {
                    const clickedBtn = await page.evaluate(() => {
                        const formBtn = document.querySelector('form button[type="submit"], button[type="submit"], form.flex button, form button');
                        if (formBtn) {
                            formBtn.click();
                            return (formBtn.innerText || 'submit').trim();
                        }
                        const allBtns = Array.from(document.querySelectorAll('button, div[role="button"], a[role="button"], input[type="submit"]'));
                        for (const b of allBtns) {
                            const t = (b.innerText || b.value || b.textContent || '').trim().toLowerCase();
                            const rect = b.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0 && (t.includes('download') || t.includes('get link') || t.includes('fetch') || t.includes('submit'))) {
                                b.click();
                                return t;
                            }
                        }
                        return null;
                    });
                    if (clickedBtn) {
                        logStatus(`Triggered Download submit button via direct DOM click ("${clickedBtn}").`);
                    }
                } catch (_) {}
            }
        }

        // Fallback: If direct navigation was not used, scan frames as safety net
        if (!formSubmitted && !tokenCaptured) {
            logStatus('Direct navigation not used; scanning frames as fallback...');
            const allFramesToTry = [targetFrame, ...page.frames().filter(f => f && f !== targetFrame)].filter(Boolean);

            for (const frame of allFramesToTry) {
                if (formSubmitted || tokenCaptured) break;

                try {
                    const inputSelectors = [
                        'input[placeholder*="Enter Diskwala Link" i]',
                        'input[placeholder*="Diskwala" i]',
                        'input[placeholder*="TeraBox" i]',
                        'input[placeholder*="terabox" i]',
                        'input[placeholder*="link" i]',
                        'input[type="text"]',
                        'input',
                    ];

                    let inEl = null;
                    for (const sel of inputSelectors) {
                        try {
                            const el = await frame.$(sel);
                            if (el) { inEl = el; break; }
                        } catch (_) {}
                    }

                    if (inEl) {
                        logStatus('Found link input field in frame. Entering link...');
                        await inEl.focus();
                        await frame.evaluate(el => el.focus(), inEl);
                        await page.keyboard.type(dummyUrl);
                        await sleep(500);
                        await page.keyboard.press('Enter');

                        try {
                            await frame.evaluate(() => {
                                const b = document.querySelector('button[type="submit"], form button, button');
                                if (b) b.click();
                            });
                        } catch (_) {}
                        formSubmitted = true;
                        break;
                    }
                } catch (_) {}
            }
        }

        // 10. Wait up to 60 seconds for the token to be intercepted (with periodic re-click fallback)
        logStatus('Listening for Bearer token on network (waiting up to 60s)...');
        const captureWaitStart = Date.now();
        let reclickDone1 = false;
        let reclickDone2 = false;

        while (!tokenCaptured && (Date.now() - captureWaitStart) < 60000) {
            const elapsed = Math.round((Date.now() - captureWaitStart) / 1000);

            if (elapsed > 0 && elapsed % 5 === 0) {
                logStatus(`Waiting for Bearer token on network (${elapsed}s elapsed)...`);
            }

            // Fallback re-press Enter and DOM click at 10s and 20s if the token has not arrived
            if (elapsed >= 10 && !reclickDone1 && !tokenCaptured) {
                reclickDone1 = true;
                logStatus('Token not yet received at 10s. Re-triggering Enter & Download button via DOM...');
                try {
                    await page.keyboard.press('Enter');
                    await page.evaluate(() => {
                        const btn = document.querySelector('form.flex button[type="submit"], form button[type="submit"], button[type="submit"], form button');
                        if (btn) btn.click();
                    });
                } catch (_) {}
                if (targetFrame) {
                    try {
                        await targetFrame.evaluate(() => {
                            const btn = document.querySelector('button[type="submit"], button');
                            if (btn) btn.click();
                        });
                    } catch (_) {}
                }
            }

            if (elapsed >= 20 && !reclickDone2 && !tokenCaptured) {
                reclickDone2 = true;
                logStatus('Token not yet received at 20s. Second re-trigger of Enter & Download button via DOM...');
                try {
                    await page.keyboard.press('Enter');
                    await page.evaluate(() => {
                        const btn = document.querySelector('form.flex button[type="submit"], form button[type="submit"], button[type="submit"], form button');
                        if (btn) btn.click();
                    });
                } catch (_) {}
                if (targetFrame) {
                    try {
                        await targetFrame.evaluate(() => {
                            const btn = document.querySelector('button[type="submit"], button');
                            if (btn) btn.click();
                        });
                    } catch (_) {}
                }
            }

            await sleep(1000);
        }

        if (tokenCaptured) {
            await sleep(1500);
            await cleanupAndExit(0, 'AUTOVNC_SUCCESS', 'Token captured and verified successfully!');
        } else {
            await cleanupAndExit(1, 'AUTOVNC_ERROR', 'MiniApp was triggered but bearer token was not emitted within 60s.');
        }

    } catch (err) {
        logStatus(`Unhandled AutoVNC error: ${err.message}`);
        await cleanupAndExit(1, 'AUTOVNC_ERROR', err.message);
    }
})();
