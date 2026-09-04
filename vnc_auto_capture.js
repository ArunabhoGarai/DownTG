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

        await setupInterception(page);

        const targetHash = TARGET_URL.includes('#') ? ('#' + TARGET_URL.split('#')[1]) : '';

        // Secondary tab tracker: if Chrome opens the target link in a secondary tab, switch to it!
        browser.on('targetcreated', async (target) => {
            if (target.type() === 'page') {
                try {
                    const newPage = await target.page();
                    if (newPage && newPage !== page) {
                        const u = newPage.url();
                        await setupInterception(newPage);
                        if (targetHash && u.includes(targetHash)) {
                            logStatus('Target chat opened in secondary tab. Bringing it to front!');
                            await newPage.bringToFront();
                            try { await page.close(); } catch (_) {}
                            page = newPage;
                        } else if (u === 'about:blank' || u.startsWith('chrome://')) {
                            await newPage.close();
                            await page.bringToFront();
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
        async function handleLaunchConfirmationModal(p) {
            try {
                // Retry loop for slow EC2 where clicks might get dropped or need multiple attempts
                for (let retry = 0; retry < 3; retry++) {
                    let clicked = false;
                    let clickedText = '';

                    // 1. Native CDP Handle search (sends hardware-level OS mouse events with isTrusted: true)
                    const btnHandles = await p.$$('button, .btn, div[role="button"], a[role="button"], .popup-button, .confirm-dialog-button');
                    for (const h of btnHandles) {
                        try {
                            const info = await p.evaluate((el) => {
                                const rect = el.getBoundingClientRect();
                                const isVisible = (rect.width > 0 && rect.height > 0) && window.getComputedStyle(el).visibility !== 'hidden';
                                const raw = (el.innerText || el.textContent || '').trim();
                                const t = raw.toLowerCase();
                                const isConfirm = (
                                    (t === 'confirm' || t.includes('confirm') || t === 'launch' || t.includes('launch') || t.includes('proceed')) &&
                                    !t.includes('cancel') &&
                                    !t.includes('close')
                                );
                                return { isConfirm, isVisible, text: raw };
                            }, h);

                            if (info && info.isConfirm && info.isVisible) {
                                await p.evaluate(el => el.scrollIntoView({ behavior: 'instant', block: 'center' }), h);
                                await sleep(250);
                                await h.click({ delay: 100 });
                                await p.evaluate(el => { if (typeof el.click === 'function') el.click(); }, h);
                                clicked = true;
                                clickedText = info.text;
                                break;
                            }
                        } catch (_) {}
                    }

                    // 2. Fallback: DOM container & synthetic event chain
                    if (!clicked) {
                        const synRes = await p.evaluate(() => {
                            const allElements = Array.from(document.querySelectorAll('*'));
                            const confirmDialog = allElements.find(el => {
                                const t = (el.innerText || '').toLowerCase();
                                return (
                                    (t.includes('would like to open its web app') || t.includes('access your ip address') || t.includes('open this web app')) &&
                                    el.children.length > 0 &&
                                    el.children.length < 15
                                );
                            });

                            let buttons = [];
                            if (confirmDialog) {
                                buttons = Array.from(confirmDialog.querySelectorAll('button, .btn, div[role="button"], a[role="button"]'));
                            }
                            if (buttons.length === 0) {
                                const modalContainers = Array.from(document.querySelectorAll(
                                    '.modal-dialog, .popup, .popup-container, .modal, div[role="dialog"], .confirm-dialog, .modal-content, .popup-body, .Modal, .popup-buttons'
                                ));
                                for (const mc of modalContainers) {
                                    buttons.push(...Array.from(mc.querySelectorAll('button, .btn, div[role="button"], a[role="button"]')));
                                }
                            }
                            if (buttons.length === 0) {
                                buttons = Array.from(document.querySelectorAll(
                                    '.popup-button, .confirm-dialog-button, .Button.confirm, button.primary, button.btn-primary, button'
                                ));
                            }

                            for (const btn of buttons) {
                                const rawText = (btn.innerText || btn.textContent || '').trim();
                                const text = rawText.toLowerCase();
                                const isConfirm = (
                                    (text === 'confirm' || text.includes('confirm') || text === 'launch' || text.includes('launch') || text.includes('proceed') || text === 'open') &&
                                    !text.includes('cancel') &&
                                    !text.includes('close')
                                );

                                if (isConfirm) {
                                    if (btn.classList.contains('hide')) {
                                        btn.classList.remove('hide');
                                        btn.style.display = 'inline-flex';
                                        btn.style.visibility = 'visible';
                                        btn.style.pointerEvents = 'auto';
                                    }
                                    btn.scrollIntoView({ behavior: 'instant', block: 'center' });
                                    const evOpts = { bubbles: true, cancelable: true, view: window };
                                    btn.dispatchEvent(new PointerEvent('pointerdown', evOpts));
                                    btn.dispatchEvent(new MouseEvent('mousedown', evOpts));
                                    btn.dispatchEvent(new PointerEvent('pointerup', evOpts));
                                    btn.dispatchEvent(new MouseEvent('mouseup', evOpts));
                                    btn.dispatchEvent(new MouseEvent('click', evOpts));
                                    if (typeof btn.click === 'function') btn.click();
                                    return { clicked: true, text: rawText };
                                }
                            }
                            return { clicked: false };
                        });

                        if (synRes && synRes.clicked) {
                            clicked = true;
                            clickedText = synRes.text;
                        }
                    }

                    if (clicked) {
                        logStatus(`Clicked launch popup "Confirm" (attempt ${retry + 1}): "${clickedText}"`);
                        // Give 1.2s for Telegram Web SPA to register on slow EC2
                        await sleep(1200);

                        // Check if modal is still open; if closed, we succeeded!
                        const stillOpen = await p.evaluate(() => {
                            const modal = document.querySelector('.modal-dialog, .popup, .confirm-dialog, div[role="dialog"]');
                            if (!modal) return false;
                            const t = (modal.innerText || '').toLowerCase();
                            return (t.includes('confirm') || t.includes('would like to open')) && !t.includes('cancel');
                        });

                        if (!stillOpen) {
                            return true;
                        }
                        logStatus('Modal still visible after click, re-triggering confirmation click...');
                    } else {
                        break;
                    }
                }
            } catch (_) {}
            return false;
        }

        // 5. Handle bottom-left context-sensitive action buttons (.chat-input.chat-input-main button: "Open Chat", "START", "UNBLOCK", etc.)
        logStatus('Checking for context-sensitive action button in bottom bar (.chat-input.chat-input-main button)...');
        try {
            for (let actionAttempt = 0; actionAttempt < 3; actionAttempt++) {
                let actionClicked = false;
                let btnText = '';

                // Try native Puppeteer element handle first (CDP hardware-level mouse event)
                const handle = await page.$('.chat-input.chat-input-main button, .chat-input-control-button, .chat-input-plate-button, button.btn-primary');
                if (handle) {
                    const info = await page.evaluate((el) => {
                        if (el.classList.contains('hide')) {
                            el.classList.remove('hide');
                            el.style.display = 'inline-flex';
                            el.style.visibility = 'visible';
                            el.style.pointerEvents = 'auto';
                            el.style.opacity = '1';
                        }
                        const raw = (el.innerText || el.textContent || '').trim();
                        const t = raw.toLowerCase();
                        const isMatch = (
                            t.includes('open chat') ||
                            t === 'open' ||
                            t.includes('open') ||
                            t.includes('start') ||
                            t.includes('restart') ||
                            t.includes('unblock') ||
                            t.includes('join') ||
                            el.classList.contains('chat-input-control-button') ||
                            el.classList.contains('chat-input-plate-button')
                        );
                        return { isMatch, raw };
                    }, handle);

                    if (info && info.isMatch && info.raw.length > 0) {
                        btnText = info.raw;
                        try {
                            await page.evaluate(el => el.scrollIntoView({ behavior: 'instant', block: 'center' }), handle);
                            await sleep(300);
                            await handle.click({ delay: 100 });
                            actionClicked = true;
                        } catch (_) {}
                    }
                }

                // Also execute synthetic click event sequence
                const synResult = await page.evaluate(() => {
                    const candidates = Array.from(document.querySelectorAll(
                        '.chat-input.chat-input-main button, .chat-input-control-button, .chat-input-plate-button, .chat-input-main button, .chat-input button, .bot-menu-button, button.btn-primary'
                    ));
                    for (const btn of candidates) {
                        const rawText = (btn.innerText || btn.textContent || '').trim();
                        const text = rawText.toLowerCase();
                        const isMatch = (
                            text.includes('open chat') ||
                            text === 'open' ||
                            text.includes('open') ||
                            text.includes('start') ||
                            text.includes('restart') ||
                            text.includes('unblock') ||
                            text.includes('join') ||
                            btn.classList.contains('chat-input-control-button') ||
                            btn.classList.contains('chat-input-plate-button')
                        );
                        if (isMatch && rawText.length > 0) {
                            if (btn.classList.contains('hide')) {
                                btn.classList.remove('hide');
                                btn.style.display = 'inline-flex';
                                btn.style.visibility = 'visible';
                                btn.style.pointerEvents = 'auto';
                                btn.style.opacity = '1';
                            }
                            btn.scrollIntoView({ behavior: 'instant', block: 'center' });
                            const evOpts = { bubbles: true, cancelable: true, view: window };
                            btn.dispatchEvent(new PointerEvent('pointerdown', evOpts));
                            btn.dispatchEvent(new MouseEvent('mousedown', evOpts));
                            btn.dispatchEvent(new PointerEvent('pointerup', evOpts));
                            btn.dispatchEvent(new MouseEvent('mouseup', evOpts));
                            btn.dispatchEvent(new MouseEvent('click', evOpts));
                            if (typeof btn.click === 'function') btn.click();
                            return { clicked: true, text: rawText };
                        }
                    }
                    return { clicked: false };
                });

                if (synResult && synResult.clicked) {
                    actionClicked = true;
                    btnText = btnText || synResult.text;
                }

                if (actionClicked) {
                    logStatus(`Clicked context action button (attempt ${actionAttempt + 1}): "${btnText}"`);
                    await sleep(3000);
                    // Check if confirmation modal immediately opened
                    await handleLaunchConfirmationModal(page);
                    break;
                } else {
                    break;
                }
            }
        } catch (e) {
            logStatus(`Context action button check: ${e.message}`);
        }

        // Check again if clicking action button triggered launch confirmation modal
        await sleep(2000);
        await handleLaunchConfirmationModal(page);

        // 6. Look for MiniApp trigger buttons (up to 35 attempts ~ 70s on slow EC2)
        logStatus('Searching for MiniApp launch button...');
        let appTriggerClicked = false;

        for (let attempt = 0; attempt < 35; attempt++) {
            if (tokenCaptured) break;

            // Check if confirmation modal already appeared
            await handleLaunchConfirmationModal(page);

            appTriggerClicked = await page.evaluate((plat) => {
                function clickWithEvents(el) {
                    if (!el) return false;
                    if (el.classList.contains('hide')) {
                        el.classList.remove('hide');
                        el.style.display = 'inline-flex';
                        el.style.visibility = 'visible';
                        el.style.pointerEvents = 'auto';
                        el.style.opacity = '1';
                    }
                    el.scrollIntoView({ behavior: 'instant', block: 'center' });
                    const evOpts = { bubbles: true, cancelable: true, view: window };
                    btnEvents(el, evOpts);
                    return true;
                }

                function btnEvents(el, evOpts) {
                    el.dispatchEvent(new PointerEvent('pointerdown', evOpts));
                    el.dispatchEvent(new MouseEvent('mousedown', evOpts));
                    el.dispatchEvent(new PointerEvent('pointerup', evOpts));
                    el.dispatchEvent(new MouseEvent('mouseup', evOpts));
                    el.dispatchEvent(new MouseEvent('click', evOpts));
                    if (typeof el.click === 'function') el.click();
                }

                // Priority A: Search inline keyboard buttons in messages
                const inlineButtons = Array.from(document.querySelectorAll(
                    '.reply-markup button, .inline-button, .InlineButton, button.Button, .reply-keyboard button'
                ));

                for (const btn of inlineButtons) {
                    const rawText = (btn.innerText || btn.textContent || '').trim();
                    const text = rawText.toLowerCase();
                    if (
                        text.includes('open') ||
                        text.includes('app') ||
                        text.includes('download') ||
                        text.includes('mini') ||
                        text.includes(plat) ||
                        text.includes('start')
                    ) {
                        return clickWithEvents(btn);
                    }
                }

                // Priority B: Search bottom bar web-app / bot-menu / action buttons (.chat-input.chat-input-main button)
                const bottomButtons = Array.from(document.querySelectorAll(
                    '.chat-input.chat-input-main button, .chat-input-control-button, .chat-input-plate-button, .bot-menu-button, button.is-web-app, .chat-secondary-action, .bot-menu, .btn-primary, button[title*="menu" i]'
                ));

                for (const btn of bottomButtons) {
                    const rawText = (btn.innerText || btn.textContent || '').trim();
                    const text = rawText.toLowerCase();
                    if (
                        text.includes('open') ||
                        text.includes('app') ||
                        text.includes('menu') ||
                        text.includes('download') ||
                        text.includes('mini') ||
                        btn.classList.contains('bot-menu-button') ||
                        btn.classList.contains('chat-input-control-button') ||
                        btn.classList.contains('chat-input-plate-button')
                    ) {
                        return clickWithEvents(btn);
                    }
                }

                // Priority C: Any visible button containing "Open" or "App"
                const allButtons = Array.from(document.querySelectorAll('button, div[role="button"], a[role="button"]'));
                const candidate = allButtons.find(b => {
                    const t = (b.innerText || b.textContent || '').toLowerCase().trim();
                    return (t.includes('open chat') || t.includes('open app') || t.includes('open diskwala') || t.includes('open terabox') || t === 'open');
                });

                if (candidate) {
                    return clickWithEvents(candidate);
                }

                return false;
            }, PLATFORM);

            if (appTriggerClicked) {
                logStatus('MiniApp trigger button clicked! Checking for confirmation modal...');
                // Also trigger native CDP click if matching button exists
                try {
                    const btnHandle = await page.$('.reply-markup button, .chat-input.chat-input-main button, .chat-input-control-button');
                    if (btnHandle) {
                        await btnHandle.click({ delay: 100 });
                    }
                } catch (_) {}

                await sleep(2500);
                await handleLaunchConfirmationModal(page);
                break;
            }

            await sleep(2000);
        }

        await sleep(3000);

        // 7. Handle Telegram Web confirmation modal ("DiskWala Video Downloader would like to open its web app...", "Confirm")
        logStatus('Checking for Telegram Web launch confirmation modal...');
        for (let i = 0; i < 10; i++) {
            const confirmed = await handleLaunchConfirmationModal(page);
            if (confirmed) {
                await sleep(2500);
                break;
            }
            await sleep(1000);
        }

        // 8. Locate the MiniApp iframe (waiting up to 90s for slow EC2)
        logStatus('Locating MiniApp iframe (waiting up to 90s for slow EC2)...');
        let targetFrame = null;
        const frameWaitStart = Date.now();

        while ((Date.now() - frameWaitStart) < 90000) {
            if (tokenCaptured) break;

            // Continuously check for and click the confirmation popup if it appears delayed
            await handleLaunchConfirmationModal(page);

            const frames = page.frames();
            for (const f of frames) {
                const frameUrl = f.url().toLowerCase();
                if (
                    frameUrl.includes('twa.') ||
                    frameUrl.includes('diskwala') ||
                    frameUrl.includes('teradownloader') ||
                    frameUrl.includes('terabox') ||
                    frameUrl.includes('miniapp') ||
                    frameUrl.includes('app')
                ) {
                    targetFrame = f;
                    break;
                }
            }

            if (targetFrame) break;

            // Also inspect frames with input elements
            for (const f of frames) {
                try {
                    const hasInput = await f.evaluate(() => Boolean(document.querySelector('input')));
                    if (hasInput) {
                        targetFrame = f;
                        break;
                    }
                } catch (_) {}
            }

            if (targetFrame) break;

            const elapsedSec = Math.round((Date.now() - frameWaitStart) / 1000);
            if (elapsedSec > 0 && elapsedSec % 15 === 0) {
                logStatus(`Scanning for MiniApp iframe (${elapsedSec}s elapsed)...`);
            }

            await sleep(2500);
        }

        if (!targetFrame) {
            logStatus('Primary iframe not isolated; scanning all frames directly...');
            targetFrame = page.mainFrame();
        } else {
            logStatus(`MiniApp iframe located: ${targetFrame.url().slice(0, 60)}...`);
        }

        // Give the iframe DOM 3.5 seconds to settle on slow EC2
        await sleep(3500);

        // 9. Enter dummy link into input field (relaxed typing & verification for EC2)
        const dummyUrl = DUMMY_LINKS[PLATFORM] || DUMMY_LINKS['diskwala'];
        logStatus(`Preparing to enter dummy link for ${PLATFORM}...`);

        let inputFoundAndFilled = false;
        const allFramesToTry = [targetFrame, ...page.frames().filter(f => f !== targetFrame)];

        for (const frame of allFramesToTry) {
            if (inputFoundAndFilled || tokenCaptured) break;

            try {
                const inputSelectors = [
                    'input[placeholder*="link" i]',
                    'input[placeholder*="url" i]',
                    'input[placeholder*="diskwala" i]',
                    'input[placeholder*="terabox" i]',
                    'input[type="text"]',
                    'input[type="url"]',
                    'input',
                    'textarea',
                ];

                let inputHandle = null;
                for (const sel of inputSelectors) {
                    try {
                        const el = await frame.$(sel);
                        if (el) {
                            inputHandle = el;
                            break;
                        }
                    } catch (_) {}
                }

                if (inputHandle) {
                    logStatus('Found link input field in MiniApp. Registering input at relaxed EC2 speed...');
                    try {
                        await frame.evaluate((el) => {
                            el.scrollIntoView({ behavior: 'instant', block: 'center' });
                        }, inputHandle);
                    } catch (_) {}
                    await sleep(600);

                    // Focus & clear field thoroughly
                    try {
                        await inputHandle.click({ clickCount: 3, delay: 60 });
                    } catch (_) {}
                    await sleep(300);

                    await frame.evaluate((el) => {
                        el.focus();
                        el.value = '';
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }, inputHandle);
                    await sleep(400);

                    // Type slowly and deliberately (60-100ms per character) so EC2 doesn't drop keystrokes
                    logStatus(`Typing link at relaxed EC2 speed: ${dummyUrl}...`);
                    for (const char of dummyUrl) {
                        if (tokenCaptured) break;
                        await inputHandle.type(char, { delay: Math.floor(Math.random() * 40) + 60 });
                    }
                    await sleep(800);

                    // Verification: check if the value was completely registered
                    const currentVal = await frame.evaluate(el => el.value, inputHandle);
                    if (currentVal !== dummyUrl) {
                        logStatus(`Input value was incomplete ("${currentVal}"). Direct injection fallback applied...`);
                        await frame.evaluate((el, val) => {
                            el.value = val;
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        }, inputHandle, dummyUrl);
                        await sleep(500);
                    }

                    // Trigger blur to ensure React/Vue field validation commits
                    await frame.evaluate((el) => {
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        el.blur();
                    }, inputHandle);
                    await sleep(800);

                    // Multi-click retry loop for the Download button (up to 4 attempts on EC2)
                    logStatus('Locating and clicking Download button (with multi-click retry)...');
                    for (let clickAttempt = 0; clickAttempt < 4; clickAttempt++) {
                        if (tokenCaptured) break;

                        // Try native Puppeteer element handle click first
                        let btnHandle = null;
                        const btnHandles = await frame.$$('button, div[role="button"], a[role="button"], .btn');
                        for (const bh of btnHandles) {
                            const isDl = await frame.evaluate((b) => {
                                const t = (b.innerText || b.textContent || '').toLowerCase().trim();
                                return (
                                    t.includes('download') ||
                                    t.includes('get') ||
                                    t.includes('submit') ||
                                    t.includes('fetch') ||
                                    t.includes('play')
                                );
                            }, bh);
                            if (isDl) {
                                btnHandle = bh;
                                break;
                            }
                        }

                        if (btnHandle) {
                            try {
                                await frame.evaluate(el => el.scrollIntoView({ behavior: 'instant', block: 'center' }), btnHandle);
                                await sleep(200);
                                await btnHandle.click({ delay: 100 });
                            } catch (_) {}
                        }

                        // Also trigger synthetic event chain
                        const clicked = await frame.evaluate(() => {
                            const candidates = Array.from(document.querySelectorAll('button, div[role="button"], a[role="button"], .btn'));
                            const btn = candidates.find(b => {
                                const t = (b.innerText || '').toLowerCase().trim();
                                return (
                                    t.includes('download') ||
                                    t.includes('get') ||
                                    t.includes('submit') ||
                                    t.includes('fetch') ||
                                    t.includes('play')
                                );
                            });

                            if (btn) {
                                btn.scrollIntoView({ behavior: 'instant', block: 'center' });
                                const evOpts = { bubbles: true, cancelable: true, view: window };
                                btn.dispatchEvent(new PointerEvent('pointerdown', evOpts));
                                btn.dispatchEvent(new MouseEvent('mousedown', evOpts));
                                btn.dispatchEvent(new PointerEvent('pointerup', evOpts));
                                btn.dispatchEvent(new MouseEvent('mouseup', evOpts));
                                btn.dispatchEvent(new MouseEvent('click', evOpts));
                                if (typeof btn.click === 'function') btn.click();
                                return true;
                            }

                            // Try button adjacent to input
                            const inputEl = document.querySelector('input');
                            if (inputEl && inputEl.parentElement) {
                                const siblingBtn = inputEl.parentElement.querySelector('button');
                                if (siblingBtn) {
                                    siblingBtn.scrollIntoView({ behavior: 'instant', block: 'center' });
                                    siblingBtn.click();
                                    return true;
                                }
                            }
                            return false;
                        });

                        if (clicked || btnHandle) {
                            inputFoundAndFilled = true;
                            logStatus(`Download button clicked (attempt ${clickAttempt + 1}). Awaiting API token...`);
                            await sleep(2500);
                            if (tokenCaptured) break;
                        }
                    }
                }
            } catch (_) {}
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

            // Fallback re-click at 10s and 20s if the token has not arrived (safeguard against laggy network)
            if (elapsed >= 10 && !reclickDone1 && !tokenCaptured && targetFrame) {
                reclickDone1 = true;
                logStatus('Token not yet received at 10s. Re-triggering Download button click...');
                try {
                    await targetFrame.evaluate(() => {
                        const b = document.querySelector('button');
                        if (b) b.click();
                    });
                } catch (_) {}
            }

            if (elapsed >= 20 && !reclickDone2 && !tokenCaptured && targetFrame) {
                reclickDone2 = true;
                logStatus('Token not yet received at 20s. Second re-trigger of Download button click...');
                try {
                    await targetFrame.evaluate(() => {
                        const b = document.querySelector('button');
                        if (b) b.click();
                    });
                } catch (_) {}
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
