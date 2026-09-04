/**
 * VNC Diskwala Token Capture Worker
 * =================================
 * Launches Chromium in GUI mode (DISPLAY=:99) with persistent user profile.
 * Navigates to Telegram Web.
 * Intercepts requests to api2.diskwala.net and extracts Bearer Authorization token.
 */
const path = require('path');
const fs = require('fs');

const TARGET_URL = process.argv[2] || 'https://web.telegram.org/a/#7802633228';
const USER_DATA_DIR = process.argv[3] || path.join(__dirname, 'data', 'tg_browser_profile');

if (!fs.existsSync(USER_DATA_DIR)) {
    fs.mkdirSync(USER_DATA_DIR, { recursive: true });
}

// Clean up stale lock/port files if any
const devtoolsPortFile = path.join(USER_DATA_DIR, 'DevToolsActivePort');
if (fs.existsSync(devtoolsPortFile)) {
    try { fs.unlinkSync(devtoolsPortFile); } catch (_) {}
}

function logStatus(msg) {
    console.log(`[STATUS] ${msg}`);
}

(async () => {
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
            console.log(JSON.stringify({ success: false, error: e.message }));
            process.exit(1);
        }
    }

    logStatus("Launching Chrome GUI on virtual desktop with persistent profile...");

    const browser = await puppeteer.launch({
        headless: false, // GUI mode for VNC!
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

    await new Promise(r => setTimeout(r, 600));

    const pages = await browser.pages();
    let page = pages[0];

    // Find the page that has TARGET_URL or use the first page
    for (const p of pages) {
        const u = p.url();
        if (u.includes('telegram.org') || u === TARGET_URL) {
            page = p;
            break;
        }
    }

    // Close any other tabs (e.g. blank tabs, newtab, about:blank)
    for (const p of pages) {
        if (p !== page) {
            try {
                const u = p.url();
                if (u === 'about:blank' || u.startsWith('chrome://')) {
                    await p.close();
                }
            } catch (_) {}
        }
    }

    // DO NOT force setViewport(1920, 1080) because fixed 1080 height causes the bottom of Telegram to be cut off!
    await page.bringToFront();

    try {
        const dims = await page.evaluate(() => {
            return {
                innerWidth: window.innerWidth,
                innerHeight: window.innerHeight,
            };
        });
        logStatus(`Viewport client area: ${dims.innerWidth}x${dims.innerHeight}`);

        if (dims.innerHeight < 820) {
            logStatus('Compact vertical space detected (<820px). Applying 90% zoom...');
            await page.evaluate(() => {
                document.documentElement.style.zoom = '90%';
            });
        }
    } catch (_) {}

    let tokenCaptured = false;

    async function setupInterception(p) {
        try {
            p.on('request', async (req) => {
                const url = req.url();
                const headers = req.headers();
                if (url.includes('api2.diskwala.net') || url.includes('/api/diskwala/')) {
                    const auth = headers['authorization'];
                    if (auth && (auth.startsWith('Bearer ') || auth.includes('query_id='))) {
                        console.log(`[DISKWALA_TOKEN_CAPTURED] ${auth}`);
                        console.log(`[TOKEN_CAPTURED] ${auth}`);
                    }
                }
                if (url.includes('apiwala.teradownloader.pro') || url.includes('/api/terabox/')) {
                    const auth = headers['authorization'];
                    if (auth && (auth.startsWith('Bearer ') || auth.includes('user='))) {
                        console.log(`[TERABOX_TOKEN_CAPTURED] ${auth}`);
                    }
                }
            });
        } catch (_) {}
    }

    await setupInterception(page);
    browser.on('targetcreated', async (target) => {
        if (target.type() === 'page') {
            try {
                const newPage = await target.page();
                if (newPage) {
                    const u = newPage.url();
                    // Close accidental blank tabs and re-focus Telegram tab
                    if (u === 'about:blank' || u.startsWith('chrome://')) {
                        await newPage.close();
                        await page.bringToFront();
                    } else {
                        await setupInterception(newPage);
                    }
                }
            } catch (_) {}
        }
    });

    logStatus(`Navigating directly to ${TARGET_URL}...`);
    try {
        await page.goto(TARGET_URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
    } catch (e) {
        logStatus(`Navigation notice: ${e.message}`);
    }

    const targetHash = TARGET_URL.includes('#') ? ('#' + TARGET_URL.split('#')[1]) : '';
    if (targetHash) {
        await new Promise(r => setTimeout(r, 1500));
        await page.evaluate((h) => {
            if (window.location.hash !== h) {
                window.location.hash = h;
            }
        }, targetHash);
    }
    await page.bringToFront();

    logStatus("Chrome is active on VNC display. Waiting for Telegram Web interaction & MiniApp token...");

    const cleanup = async () => {
        try { await browser.close(); } catch (_) {}
        process.exit(0);
    };
    process.on('SIGINT', cleanup);
    process.on('SIGTERM', cleanup);
})();
