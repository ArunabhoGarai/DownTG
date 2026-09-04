/**
 * Local Test Script for AutoVNC MiniApp Navigation & Token Interception
 * ======================================================================
 * Tests Puppeteer navigation directly on your local machine:
 * - Launches Chrome in visible GUI mode (headed).
 * - Navigates directly to Diskwala (#7802633228) or TeraBox (#7802009139).
 * - Ensures the target chat is the active, focused tab (no blank or secondary tabs).
 * - Handles slow/laggy Telegram Web SPA rendering.
 * - Triggers the MiniApp, confirms launch modal, inputs dummy link, and clicks download.
 * - Intercepts and displays the captured Bearer token.
 * 
 * Usage:
 *   node test_autovnc_local.js diskwala [optional_custom_url]
 *   node test_autovnc_local.js tera [optional_custom_url]
 */

const path = require('path');
const fs = require('fs');

const PLATFORM = (process.argv[2] || 'diskwala').toLowerCase();

// User-provided direct Telegram Web A chat URLs
const DEFAULT_URLS = {
    diskwala: 'https://web.telegram.org/a/#7802633228',
    tera: 'https://web.telegram.org/a/#7802009139',
    terabox: 'https://web.telegram.org/a/#7802009139',
};

const TARGET_URL = process.argv[3] || DEFAULT_URLS[PLATFORM] || DEFAULT_URLS['diskwala'];
const USER_DATA_DIR = path.join(__dirname, 'data', 'tg_browser_profile');

const DUMMY_LINKS = {
    diskwala: 'https://www.diskwala.com/app/6a992e7206ba7ea03daf9137',
    tera: 'https://www.terabox.app/sharing/link?surl=shl4DnwTd2xTki0tHnAOyQ',
    terabox: 'https://www.terabox.app/sharing/link?surl=shl4DnwTd2xTki0tHnAOyQ',
};

if (!fs.existsSync(USER_DATA_DIR)) {
    fs.mkdirSync(USER_DATA_DIR, { recursive: true });
}

function log(msg) {
    const ts = new Date().toLocaleTimeString();
    console.log(`[${ts}] ${msg}`);
}

function sleep(ms) {
    return new Promise(r => setTimeout(r, ms));
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
        log(`💾 Token saved directly to .env (${key})`);
    } catch (e) {
        log(`Failed saving to .env: ${e.message}`);
    }
}

function findChromeExecutable() {
    if (process.platform === 'win32') {
        const candidates = [
            'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
            'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
            path.join(process.env.LOCALAPPDATA || '', 'Google', 'Chrome', 'Application', 'chrome.exe'),
        ];
        for (const c of candidates) {
            if (fs.existsSync(c)) return c;
        }
    }
    return undefined;
}

function findEdgeExecutable() {
    if (process.platform === 'win32') {
        const candidates = [
            'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
            'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
            path.join(process.env.LOCALAPPDATA || '', 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
        ];
        for (const c of candidates) {
            if (fs.existsSync(c)) return c;
        }
    }
    return undefined;
}

function killLingeringProfileChrome() {
    if (process.platform !== 'win32') return;
    try {
        const { execSync } = require('child_process');
        execSync(
            'powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \\"Name = \'chrome.exe\'\\" | Where-Object { $_.CommandLine -like \'*tg_browser_profile*\' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"',
            { stdio: 'ignore', timeout: 5000 }
        );
    } catch (_) {}
}

function cleanLockFiles() {
    const filesToClean = [
        path.join(USER_DATA_DIR, 'DevToolsActivePort'),
        path.join(USER_DATA_DIR, 'SingletonLock'),
        path.join(USER_DATA_DIR, 'SingletonCookie'),
        path.join(USER_DATA_DIR, 'SingletonSocket'),
        path.join(USER_DATA_DIR, 'lockfile'),
    ];
    for (const f of filesToClean) {
        if (fs.existsSync(f)) {
            try { fs.unlinkSync(f); } catch (_) {}
        }
    }
}

async function launchBrowserWithFallback(puppeteer, chromePath) {
    const getOptions = (execPath) => ({
        headless: false,
        executablePath: execPath,
        userDataDir: USER_DATA_DIR,
        defaultViewport: null,
        args: [
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--start-maximized',
            '--disable-dev-shm-usage',
            '--disable-infobars',
            '--no-first-run',
            '--no-default-browser-check',
        ],
        ignoreDefaultArgs: ['--enable-automation'],
        timeout: 40000,
    });

    // Terminate any orphan chrome using this profile and clean lock files
    killLingeringProfileChrome();
    cleanLockFiles();

    // Attempt 1: System Chrome
    if (chromePath) {
        try {
            log(`Launching system Chrome GUI: ${chromePath}...`);
            return await puppeteer.launch(getOptions(chromePath));
        } catch (err) {
            log(`⚠️ Primary system Chrome launch failed: ${err.message}`);
            log('Cleaning locks and attempting bundled Chromium fallback...');
            await sleep(1500);
            killLingeringProfileChrome();
            cleanLockFiles();
        }
    }

    // Attempt 2: Bundled Chromium
    try {
        log('Launching Puppeteer bundled browser...');
        return await puppeteer.launch(getOptions(undefined));
    } catch (err) {
        log(`⚠️ Bundled browser launch failed: ${err.message}`);
        await sleep(1500);
        cleanLockFiles();
    }

    // Attempt 3: Microsoft Edge
    const edgePath = findEdgeExecutable();
    if (edgePath) {
        try {
            log(`Launching Microsoft Edge fallback: ${edgePath}...`);
            return await puppeteer.launch(getOptions(edgePath));
        } catch (err) {
            log(`⚠️ Edge fallback failed: ${err.message}`);
        }
    }

    throw new Error('All browser launch attempts failed. Please ensure no hanging Chrome processes are locking the profile.');
}

(async () => {
    try {
        log('====================================================');
        log(`🧪 AutoVNC Local Navigation Test: ${PLATFORM.toUpperCase()}`);
        log(`🎯 Target URL: ${TARGET_URL}`);
        log(`📁 Profile Dir: ${USER_DATA_DIR}`);
        log('====================================================');

        let puppeteer;
        try {
            const { addExtra } = require('puppeteer-extra');
            const StealthPlugin = require('puppeteer-extra-plugin-stealth');
            const puppeteerVanillaModule = await import('puppeteer');
            const puppeteerVanilla = puppeteerVanillaModule.default || puppeteerVanillaModule;

            puppeteer = addExtra(puppeteerVanilla);
            puppeteer.use(StealthPlugin());
        } catch (err) {
            log(`Error loading puppeteer-extra: ${err.message}. Using vanilla puppeteer...`);
            const p = await import('puppeteer');
            puppeteer = p.default || p;
        }

        const chromePath = findChromeExecutable();
        const browser = await launchBrowserWithFallback(puppeteer, chromePath);

    // Wait 1 second for Chrome startup to settle
    await sleep(1000);

    const initialPages = await browser.pages();
    log(`Chrome started with ${initialPages.length} tab(s).`);

    let mainPage = initialPages[0];

    // Close any extra tabs so ONLY 1 tab remains
    for (let i = 1; i < initialPages.length; i++) {
        try {
            log(`Closing extra tab ${i}: ${initialPages[i].url()}`);
            await initialPages[i].close();
        } catch (_) {}
    }

    // DO NOT force setViewport(1920, 1080) because fixed 1080 height on screens
    // with taskbars/toolbars causes the bottom of Telegram (input/buttons) to be cut off!
    // With defaultViewport: null, Chrome automatically matches the full maximized window area.
    await mainPage.bringToFront();

    try {
        const dims = await mainPage.evaluate(() => {
            return {
                innerWidth: window.innerWidth,
                innerHeight: window.innerHeight,
                availHeight: window.screen.availHeight || window.screen.height,
            };
        });
        log(`Detected viewport area: ${dims.innerWidth}x${dims.innerHeight} (Screen available: ${dims.availHeight}h)`);

        // If innerHeight is restricted (e.g. laptop 768p or 125% Windows scaling), scale Telegram Web slightly so all bottom buttons fit easily
        if (dims.innerHeight < 820) {
            log('Compact vertical space detected (<820px). Applying 90% zoom so bottom controls & MiniApp are fully visible...');
            await mainPage.evaluate(() => {
                document.documentElement.style.zoom = '90%';
            });
        }
    } catch (_) {}

    let tokenCaptured = false;

    const isTera = (PLATFORM === 'tera' || PLATFORM === 'terabox');
    const targetEnvKey = isTera ? 'TERABOX_BEARER_TOKEN' : 'DISKWALA_BEARER_TOKEN';
    const targetTag = isTera ? '[TERABOX_TOKEN_CAPTURED]' : '[DISKWALA_TOKEN_CAPTURED]';
    const targetName = isTera ? 'TeraBox' : 'Diskwala';

    // 1. Setup network token interception across all pages
    async function setupInterception(p) {
        try {
            p.on('request', async (req) => {
                const url = req.url();
                const headers = req.headers();
                const auth = headers['authorization'] || headers['Authorization'];

                if (!auth) return;
                if (!auth.startsWith('Bearer ') && !auth.includes('query_id=') && !auth.includes('user=')) return;

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
                        log(`🎉 ${targetTag}`);
                        console.log(`\n--- CAPTURED ${targetName.toUpperCase()} BEARER TOKEN ---`);
                        console.log(auth);
                        console.log('--------------------------------------\n');
                        saveTokenToEnv(targetEnvKey, auth);
                    }
                }
            });
        } catch (_) {}
    }

    await setupInterception(mainPage);

    // Watcher: if any new tab opens, bring it to front or intercept
    browser.on('targetcreated', async (target) => {
        if (target.type() === 'page') {
            try {
                const newPage = await target.page();
                if (newPage && newPage !== mainPage) {
                    const u = newPage.url();
                    log(`Secondary tab spawned: ${u}`);
                    await setupInterception(newPage);
                    if (u.includes('#7802') || u.includes('web.telegram.org')) {
                        log('Bringing target secondary tab to front...');
                        await newPage.bringToFront();
                    } else if (u === 'about:blank' || u.startsWith('chrome://')) {
                        await newPage.close();
                        await mainPage.bringToFront();
                    }
                }
            } catch (_) {}
        }
    });

    // 2. Navigate directly to TARGET_URL on mainPage
    log(`Navigating main tab directly to: ${TARGET_URL}...`);
    try {
        await mainPage.goto(TARGET_URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
    } catch (e) {
        log(`Navigation notice: ${e.message}`);
    }
    await mainPage.bringToFront();

    // Ensure hash is set in SPA
    const targetHash = TARGET_URL.includes('#') ? ('#' + TARGET_URL.split('#')[1]) : '';
    if (targetHash) {
        await sleep(1500);
        await mainPage.evaluate((h) => {
            if (window.location.hash !== h) {
                window.location.hash = h;
            }
        }, targetHash);
    }

    // 3. Check login state
    log('Checking Telegram Web session...');
    await sleep(3000);

    const isLoginScreen = await mainPage.evaluate(() => {
        const qr = document.querySelector('.qr-code, canvas, .qr-container, .auth-qr');
        const phone = document.querySelector('input[type="tel"], .input-field-input, #sign-in-phone-number');
        const titles = Array.from(document.querySelectorAll('h1, h2, h3, h4, div')).map(el => (el.innerText || '').toLowerCase());
        const hasText = titles.some(t => t.includes('log in to telegram') || t.includes('scan from mobile'));
        return Boolean((qr || phone) && hasText);
    });

    if (isLoginScreen) {
        log('⚠️ Telegram Web is NOT logged in yet in this profile!');
        log('👉 Please scan the QR code or log in with your phone in the open Chrome window.');
        log('Waiting up to 90 seconds for you to log in...');
        for (let i = 0; i < 45; i++) {
            await sleep(2000);
            const stillLogin = await mainPage.evaluate(() => {
                return Boolean(document.querySelector('.qr-code, canvas, input[type="tel"]'));
            });
            if (!stillLogin) {
                log('🎉 Login detected! Proceeding with navigation...');
                break;
            }
        }
    }

    // 4. Wait patiently for chat container to render (Telegram Web can take 15-30s on slow connections)
    log('Waiting for Telegram Web chat interface to load...');
    let chatLoaded = false;
    for (let i = 0; i < 30; i++) {
        chatLoaded = await mainPage.evaluate(() => {
            return Boolean(
                document.querySelector('.chat, .messages-container, .bubbles, .middle-column, .chat-input, .MessageList, #column-center')
            );
        });
        if (chatLoaded) break;
        await sleep(1500);
    }

    if (!chatLoaded) {
        log('Chat container did not finish rendering within 45s. Attempting to force hash switch...');
        if (targetHash) {
            await mainPage.evaluate((h) => { window.location.hash = h; }, targetHash);
        }
    } else {
        log('✅ Chat interface loaded!');
    }

    await sleep(3000);

    // Ensure message history is scrolled down
    try {
        await mainPage.evaluate(() => {
            const bubbles = document.querySelector('.middle-column, .messages-container, .bubbles, .MessageList');
            if (bubbles) bubbles.scrollTop = bubbles.scrollHeight;
        });
    } catch (_) {}

    // Reusable helper to detect and confirm Telegram Web launch popups ("Open this web app?", "Confirm", "Launch")
    async function handleLaunchConfirmationModal(p) {
        try {
            // First: Look for matching button handles directly with Puppeteer native CDP
            // Native handle clicks have isTrusted: true and pass through React/Preact synthetic events flawlessly
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
                        await h.click({ delay: 50 });
                        await p.evaluate(el => { if (typeof el.click === 'function') el.click(); }, h);
                        log(`✅ Confirmed launch popup via native CDP handle: "${info.text}"`);
                        return true;
                    }
                } catch (_) {}
            }

            // Second: Container-based text matching & synthetic event chain
            const confirmed = await p.evaluate(() => {
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

                    // Specifically match "Confirm", "Launch", "Open", "Proceed" but strictly exclude "Cancel"
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
                        if (typeof btn.click === 'function') {
                            btn.click();
                        }
                        return { clicked: true, text: rawText };
                    }
                }
                return { clicked: false };
            });

            if (confirmed && confirmed.clicked) {
                log(`✅ Confirmed launch popup via synthetic events: "${confirmed.text}"`);
                return true;
            }
        } catch (_) {}
        return false;
    }

    // 5. Handle bottom-left context-sensitive action buttons (.chat-input.chat-input-main button: "Open Chat", "START", "UNBLOCK", etc.)
    log('Checking for context-sensitive action button in bottom bar (.chat-input.chat-input-main button)...');
    try {
        const actionResult = await mainPage.evaluate(() => {
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
                    // Remove 'hide' class and ensure visibility so the click event is fully processed
                    if (btn.classList.contains('hide')) {
                        btn.classList.remove('hide');
                        btn.style.display = 'inline-flex';
                        btn.style.visibility = 'visible';
                        btn.style.pointerEvents = 'auto';
                        btn.style.opacity = '1';
                    }

                    btn.scrollIntoView({ behavior: 'smooth', block: 'center' });

                    // Dispatch full pointer and mouse event chain
                    const evOpts = { bubbles: true, cancelable: true, view: window };
                    btn.dispatchEvent(new PointerEvent('pointerdown', evOpts));
                    btn.dispatchEvent(new MouseEvent('mousedown', evOpts));
                    btn.dispatchEvent(new PointerEvent('pointerup', evOpts));
                    btn.dispatchEvent(new MouseEvent('mouseup', evOpts));
                    btn.dispatchEvent(new MouseEvent('click', evOpts));
                    if (typeof btn.click === 'function') {
                        btn.click();
                    }

                    return { clicked: true, text: rawText, classes: btn.className };
                }
            }
            return { clicked: false };
        });

        if (actionResult && actionResult.clicked) {
            log(`✅ Clicked context action button: "${actionResult.text}" (Classes: ${actionResult.classes})`);
            await sleep(3500);
        } else {
            // Also attempt native Puppeteer handle click if element exists
            const handle = await mainPage.$('.chat-input.chat-input-main button, .chat-input-control-button, .chat-input-plate-button');
            if (handle) {
                const btnInfo = await mainPage.evaluate((el) => {
                    if (el.classList.contains('hide')) {
                        el.classList.remove('hide');
                        el.style.display = 'inline-flex';
                        el.style.visibility = 'visible';
                        el.style.pointerEvents = 'auto';
                    }
                    return (el.innerText || el.textContent || '').trim();
                }, handle);
                try {
                    await handle.click({ delay: 50 });
                    log(`✅ Clicked action button via Puppeteer handle: "${btnInfo}"`);
                    await sleep(3500);
                } catch (_) {}
            }
        }
    } catch (e) {
        log(`Context action button check: ${e.message}`);
    }

    // Check if clicking the action button immediately triggered a launch confirmation modal
    await sleep(1500);
    await handleLaunchConfirmationModal(mainPage);

    // 6. Search for MiniApp launch button
    log(`Searching for ${PLATFORM} MiniApp launch button...`);
    let appTriggerClicked = false;

    for (let attempt = 0; attempt < 20; attempt++) {
        if (tokenCaptured) break;

        // Check if confirmation modal already appeared
        await handleLaunchConfirmationModal(mainPage);

        appTriggerClicked = await mainPage.evaluate((plat) => {
            function clickWithEvents(el) {
                if (!el) return false;
                if (el.classList.contains('hide')) {
                    el.classList.remove('hide');
                    el.style.display = 'inline-flex';
                    el.style.visibility = 'visible';
                    el.style.pointerEvents = 'auto';
                    el.style.opacity = '1';
                }
                el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                const evOpts = { bubbles: true, cancelable: true, view: window };
                el.dispatchEvent(new PointerEvent('pointerdown', evOpts));
                el.dispatchEvent(new MouseEvent('mousedown', evOpts));
                el.dispatchEvent(new PointerEvent('pointerup', evOpts));
                el.dispatchEvent(new MouseEvent('mouseup', evOpts));
                el.dispatchEvent(new MouseEvent('click', evOpts));
                if (typeof el.click === 'function') {
                    el.click();
                }
                return true;
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

            // Priority C: Any button on page containing "Open"
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
            log('✅ MiniApp trigger button clicked!');
            break;
        }

        await sleep(2000);
    }

    await sleep(2500);

    // 7. Handle Telegram Web confirmation popup ("DiskWala Video Downloader would like to open its web app...", "Confirm")
    log('Checking for Telegram Web launch confirmation modal...');
    for (let i = 0; i < 6; i++) {
        const confirmed = await handleLaunchConfirmationModal(mainPage);
        if (confirmed) {
            await sleep(2000);
            break;
        }
        await sleep(1000);
    }

    // 8. Locate the MiniApp iframe
    log('Scanning for MiniApp iframe (waiting up to 45s)...');
    let targetFrame = null;
    const frameWaitStart = Date.now();

    while ((Date.now() - frameWaitStart) < 45000) {
        if (tokenCaptured) break;

        // Continuously check for and click the confirmation popup if it appears delayed
        await handleLaunchConfirmationModal(mainPage);

        const frames = mainPage.frames();
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

        // Also check if any frame contains an input
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
        await sleep(2000);
    }

    if (!targetFrame) {
        log('Iframe not isolated; searching main frame directly...');
        targetFrame = mainPage.mainFrame();
    } else {
        log(`✅ MiniApp iframe located: ${targetFrame.url()}`);
    }

    await sleep(2500);

    // 9. Enter dummy link into input
    const dummyUrl = DUMMY_LINKS[PLATFORM] || DUMMY_LINKS['diskwala'];
    log(`Entering dummy link for ${PLATFORM}: ${dummyUrl}...`);

    let inputFoundAndFilled = false;
    const allFramesToTry = [targetFrame, ...mainPage.frames().filter(f => f !== targetFrame)];

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
                try {
                    await frame.evaluate((el) => {
                        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    }, inputHandle);
                } catch (_) {}
                await inputHandle.click();
                await sleep(400);

                await frame.evaluate((el) => {
                    el.value = '';
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }, inputHandle);

                // Type with human delays
                for (const char of dummyUrl) {
                    await inputHandle.type(char, { delay: Math.floor(Math.random() * 40) + 30 });
                }
                await sleep(800);

                // Click Download button
                log('Clicking Download button in MiniApp...');
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
                        btn.scrollIntoView({ behavior: 'smooth', block: 'center' });
                        btn.click();
                        return true;
                    }

                    const inputEl = document.querySelector('input');
                    if (inputEl && inputEl.parentElement) {
                        const siblingBtn = inputEl.parentElement.querySelector('button');
                        if (siblingBtn) {
                            siblingBtn.scrollIntoView({ behavior: 'smooth', block: 'center' });
                            siblingBtn.click();
                            return true;
                        }
                    }
                    return false;
                });

                if (clicked) {
                    inputFoundAndFilled = true;
                    log('✅ Download button clicked! Awaiting API token...');
                }
            }
        } catch (_) {}
    }

    // 10. Wait for token to be intercepted
    log('Listening for Bearer token on network...');
    const waitStart = Date.now();
    while (!tokenCaptured && (Date.now() - waitStart) < 30000) {
        await sleep(1000);
    }

    if (tokenCaptured) {
        log('🎉 TEST SUCCESSFUL: Bearer token was captured successfully!');
    } else {
        log('⚠️ Finished waiting. If the token was not captured, verify if your Telegram Web session is logged in.');
    }

    console.log('\n====================================================');
    console.log(' Browser is open. Press ENTER in this console to exit...');
    console.log('====================================================\n');

    // Wait for ENTER key from user (or auto-exit after 3 minutes)
    await Promise.race([
        new Promise(resolve => {
            process.stdin.resume();
            process.stdin.once('data', resolve);
        }),
        sleep(180000)
    ]);

    try {
        await browser.close();
    } catch (_) {}
    log('Done.');
} catch (fatalErr) {
    log(`❌ ERROR ENCOUNTERED: ${fatalErr.message}`);
    console.error(fatalErr);
    log('If Chrome failed to launch, close any running Chrome windows and try again.');
}
})();
