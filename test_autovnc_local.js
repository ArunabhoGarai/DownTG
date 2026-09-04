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

    // Number of steps based on distance (min 15, max 35)
    const steps = Math.max(15, Math.min(35, Math.floor(distance / 20)));

    // Generate random control point for natural curved arc
    const midX = (startX + targetX) / 2;
    const midY = (startY + targetY) / 2;
    const perpX = -dy / distance;
    const perpY = dx / distance;
    const curveOffset = (Math.random() - 0.5) * Math.min(distance * 0.3, 100);
    const ctrlX = midX + perpX * curveOffset;
    const ctrlY = midY + perpY * curveOffset;

    for (let i = 1; i <= steps; i++) {
        const t = i / steps;
        // Ease-in-out curve
        const ease = t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t;

        // Quadratic Bézier
        const u = 1 - ease;
        const x = u * u * startX + 2 * u * ease * ctrlX + ease * ease * targetX;
        const y = u * u * startY + 2 * u * ease * ctrlY + ease * ease * targetY;

        // Micro-jitter
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
        // First try native boundingBox()
        let box = await elementHandle.boundingBox();
        if (box && box.width > 0 && box.height > 0) {
            return {
                x: box.x,
                y: box.y,
                width: box.width,
                height: box.height,
            };
        }

        // If inside an iframe and boundingBox was null, calculate via frameElement offset
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

        await sleep(200);

        const coords = await getElementViewportCoords(page, elementHandle, frame);

        if (coords && coords.width > 0 && coords.height > 0) {
            // Target a natural random point inside the element (inner 60% box)
            const targetX = coords.x + coords.width * (0.2 + Math.random() * 0.6);
            const targetY = coords.y + coords.height * (0.2 + Math.random() * 0.6);

            // Move mouse along human curved trajectory
            await humanMouseMove(page, targetX, targetY);

            // Hover reaction pause
            await sleep(Math.floor(Math.random() * 80) + 60);

            // Physical mouse down
            await page.mouse.down({ button: 'left' });

            // Human click hold time (50-110ms)
            await sleep(Math.floor(Math.random() * 50) + 60);

            // Physical mouse up
            await page.mouse.up({ button: 'left' });

            log(`🖱️ Real mouse clicked on ${label} at (${Math.round(targetX)}, ${Math.round(targetY)})`);
            return true;
        } else {
            log(`⚠️ Bounding box not resolved for ${label}, falling back to handle.click()...`);
            await elementHandle.click({ delay: Math.floor(Math.random() * 50) + 60 });
            return true;
        }
    } catch (err) {
        log(`humanClick warning for ${label}: ${err.message}. Trying direct handle click...`);
        try {
            await elementHandle.click({ delay: 60 });
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

    // 1. Click input box with real mouse to establish native focus
    await humanClick(page, inputHandle, frame, 'input field');
    await sleep(350);

    // 2. Clear input using real CDP keyboard shortcuts (Ctrl+A -> Backspace)
    await page.keyboard.down('Control');
    await page.keyboard.press('KeyA');
    await page.keyboard.up('Control');
    await sleep(100);
    await page.keyboard.press('Backspace');
    await sleep(200);

    // 3. Type character by character with realistic keystroke delays (40-85ms)
    for (const char of text) {
        await page.keyboard.type(char, { delay: Math.floor(Math.random() * 45) + 40 });
    }
    await sleep(400);

    // 4. Verify value
    const targetContext = frame || page;
    const value = await targetContext.evaluate(el => el.value, inputHandle);
    if (value !== text) {
        log(`Notice: input value had discrepancy ("${value}"). Correcting...`);
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
    log(`🎯 Moving real mouse along Bézier curve to (${Math.round(targetX)}, ${Math.round(targetY)}) for ${label}...`);
    await humanMouseMove(page, targetX, targetY);
    await sleep(Math.floor(Math.random() * 60) + 70); // Hover reaction pause

    await page.mouse.down({ button: 'left' });
    await sleep(Math.floor(Math.random() * 40) + 70); // Human click hold time (70-110ms)
    await page.mouse.up({ button: 'left' });

    log(`🖱️ Real mouse clicked at (${Math.round(targetX)}, ${Math.round(targetY)}) [${label}]`);
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

    await mainPage.evaluateOnNewDocument(() => {
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined,
        });
        if (!window.chrome) {
            window.chrome = { runtime: {} };
        }
    });

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

    mainPage.on('framenavigated', (frame) => {
        try {
            const u = frame.url();
            if (isSignedMiniAppUrl(u, PLATFORM) && !miniappUrl) {
                log(`🎯 Captured signed MiniApp URL from frame navigation: ${u.slice(0, 80)}...`);
                miniappUrl = u;
            }
        } catch (_) {}
    });

    mainPage.on('request', (req) => {
        try {
            const u = req.url();
            if (isSignedMiniAppUrl(u, PLATFORM) && !miniappUrl) {
                log(`🎯 Captured signed MiniApp URL from network request: ${u.slice(0, 80)}...`);
                miniappUrl = u;
            }
        } catch (_) {}
    });

    await setupInterception(mainPage);

    // Watcher: if any new tab opens, bring it to front or intercept
    browser.on('targetcreated', async (target) => {
        if (target.type() === 'page') {
            try {
                const newPage = await target.page();
                if (newPage && newPage !== mainPage) {
                    const u = newPage.url();
                    log(`Secondary tab spawned: ${u}`);
                    if (isSignedMiniAppUrl(u, PLATFORM) && !miniappUrl) {
                        log(`🎯 Captured signed MiniApp URL from spawned tab: ${u.slice(0, 80)}...`);
                        miniappUrl = u;
                    }
                    newPage.on('framenavigated', (frame) => {
                        try {
                            const fu = frame.url();
                            if (isSignedMiniAppUrl(fu, PLATFORM) && !miniappUrl) {
                                log(`🎯 Captured signed MiniApp URL from spawned tab frame: ${fu.slice(0, 80)}...`);
                                miniappUrl = fu;
                            }
                        } catch (_) {}
                    });
                    await setupInterception(newPage);
                    if (u.includes('#7802') || u.includes('web.telegram.org')) {
                        log('Bringing target secondary tab to front...');
                        await newPage.bringToFront();
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
                log(`✅ Confirmed launch popup via DOM click: "${confirmedText}"`);
                return true;
            }
        } catch (_) {}
        return false;
    }

    // 5. Inject MutationObserver into Telegram Web DOM to instantly catch signed MiniApp iframe insertion
    try {
        await mainPage.evaluate(() => {
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
    log(`Executing the single hardware click on Open button at coordinates (X: 794, Y: 1008)...`);
    try {
        const openCoords = await resolveAdaptiveCoords(mainPage, DISKWALA_COORDS.openButton);
        await humanClickCoords(mainPage, openCoords.x, openCoords.y, 'Open button');
        log('🛑 First click executed. All further automated mouse clicks stopped.');
    } catch (e) {
        log(`Open button click notice: ${e.message}`);
    }

    await sleep(2000);
    await handleLaunchConfirmationModal(mainPage);

    // 8. Locate and isolate the SIGNED MiniApp iframe URL (must contain tgWebAppData=)
    log(`Locating and isolating signed ${targetName} MiniApp URL with tgWebAppData (waiting up to 60s)...`);
    let targetFrame = null;
    const frameWaitStart = Date.now();

    while (!miniappUrl && (Date.now() - frameWaitStart) < 60000) {
        if (tokenCaptured) break;

        // Continuously check for and click the confirmation popup if it appears delayed
        await handleLaunchConfirmationModal(mainPage);

        // Check MutationObserver immediate capture
        try {
            const obsUrl = await mainPage.evaluate(() => window.__capturedSignedMiniAppUrl);
            if (obsUrl && isSignedMiniAppUrl(obsUrl, PLATFORM)) {
                log(`🎯 Captured signed MiniApp URL from MutationObserver: ${obsUrl.slice(0, 80)}...`);
                miniappUrl = obsUrl;
                break;
            }
        } catch (_) {}

        // Check active frames
        const frames = mainPage.frames();
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
            const domSignedUrl = await mainPage.evaluate((plat) => {
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

        // Fallback: If 6 seconds elapsed after first click and no modal/signed URL appeared,
        // trigger the WebApp button via clean non-intrusive DOM click
        const elapsedSec = Math.round((Date.now() - frameWaitStart) / 1000);
        if (elapsedSec >= 6 && elapsedSec % 6 === 0 && !miniappUrl) {
            try {
                await mainPage.evaluate((plat) => {
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

        if (elapsedSec > 0 && elapsedSec % 10 === 0) {
            log(`Waiting for signed MiniApp URL with tgWebAppData= (${elapsedSec}s elapsed)...`);
        }

        await sleep(1500);
    }

    // 9. Navigate directly to isolated MiniApp URL and fill the download form
    const dummyUrl = DUMMY_LINKS[PLATFORM] || DUMMY_LINKS['diskwala'];
    let formSubmitted = false;

    if (miniappUrl) {
        log(`🎯 Isolated MiniApp URL: ${miniappUrl.slice(0, 80)}...`);
        log('Navigating directly to isolated MiniApp URL...');
        try {
            await mainPage.goto(miniappUrl, { waitUntil: 'domcontentloaded', timeout: 45000 });
        } catch (navErr) {
            log(`Direct navigation notice: ${navErr.message}. Continuing...`);
        }
        await sleep(2500);

        // Locate the input field on the isolated MiniApp page
        log('Searching for download form input on isolated MiniApp page...');
        let inputHandle = null;
        for (let waitAttempt = 0; waitAttempt < 20; waitAttempt++) {
            if (tokenCaptured) break;
            try {
                inputHandle = await mainPage.$(
                    'input[placeholder*="Enter Diskwala Link" i], input[placeholder*="Diskwala" i], input[placeholder*="TeraBox" i], input[placeholder*="terabox" i], input[placeholder*="link" i], input[placeholder*="url" i], form.flex input[type="text"], form input[type="text"], form input[type="url"], input[type="text"], input[type="url"], input'
                );
                if (inputHandle) {
                    const isVis = await mainPage.evaluate(el => {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    }, inputHandle);
                    if (isVis) break;
                }
            } catch (_) {}
            await sleep(1000);
        }

        if (inputHandle) {
            log(`Found download link form input. Focusing and entering dummy link...`);
            await inputHandle.focus();
            await mainPage.evaluate(el => el.focus(), inputHandle);
            await sleep(350);

            // Clear input via CDP keyboard
            await mainPage.keyboard.down('Control');
            await mainPage.keyboard.press('KeyA');
            await mainPage.keyboard.up('Control');
            await sleep(100);
            await mainPage.keyboard.press('Backspace');
            await sleep(150);

            log(`Entering dummy link via realistic keystrokes: ${dummyUrl}...`);
            for (const char of dummyUrl) {
                await mainPage.keyboard.type(char, { delay: Math.floor(Math.random() * 35) + 40 });
            }
            await sleep(400);

            // Ensure value is set in DOM
            const val = await mainPage.evaluate(el => el.value, inputHandle);
            if (!val || val !== dummyUrl) {
                log('Syncing dummy link value into DOM as safety...');
                await mainPage.evaluate((el, v) => {
                    el.value = v;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }, inputHandle, dummyUrl);
            }
            await sleep(300);

            // Hit Enter to submit form
            log('Submitting form: hitting ENTER...');
            await mainPage.keyboard.press('Enter');
            formSubmitted = true;
            await sleep(1000);

            // Also click Download submit button via direct DOM click (no mouse movement)
            try {
                const clickedBtn = await mainPage.evaluate(() => {
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
                    log(`Triggered Download submit button via direct DOM click ("${clickedBtn}").`);
                }
            } catch (_) {}
        }
    }

    // Fallback: If direct navigation was not used, scan frames as safety net
    if (!formSubmitted && !tokenCaptured) {
        log('Direct navigation not used; scanning frames as fallback...');
        const allFramesToTry = [targetFrame, ...mainPage.frames().filter(f => f && f !== targetFrame)].filter(Boolean);

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
                    log('Found link input field in frame. Entering link...');
                    await inEl.focus();
                    await frame.evaluate(el => el.focus(), inEl);
                    await mainPage.keyboard.type(dummyUrl);
                    await sleep(500);
                    await mainPage.keyboard.press('Enter');

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
    log('Listening for Bearer token on network (waiting up to 60s)...');
    const captureWaitStart = Date.now();
    let reclickDone1 = false;
    let reclickDone2 = false;

    while (!tokenCaptured && (Date.now() - captureWaitStart) < 60000) {
        const elapsed = Math.round((Date.now() - captureWaitStart) / 1000);

        if (elapsed > 0 && elapsed % 5 === 0) {
            log(`Waiting for Bearer token on network (${elapsed}s elapsed)...`);
        }

        // Fallback re-press Enter and DOM click at 10s and 20s if the token has not arrived
        if (elapsed >= 10 && !reclickDone1 && !tokenCaptured) {
            reclickDone1 = true;
            log('Token not yet received at 10s. Re-triggering Enter & Download button via DOM...');
            try {
                await mainPage.keyboard.press('Enter');
                await mainPage.evaluate(() => {
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
            log('Token not yet received at 20s. Second re-trigger of Enter & Download button via DOM...');
            try {
                await mainPage.keyboard.press('Enter');
                await mainPage.evaluate(() => {
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
