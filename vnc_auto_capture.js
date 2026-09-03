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
const TARGET_URL = process.argv[3] || 'https://web.telegram.org/a/';
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

function logStatus(msg) {
    console.log(`[AUTOVNC_STATUS] ${msg}`);
}

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

// 240-second watchdog timer to handle laggy VNC environments
const HARD_TIMEOUT_MS = 240 * 1000;
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
    logStatus('Operation timed out (exceeded 4 minutes). Aborting...');
    await cleanupAndExit(1, 'AUTOVNC_ERROR', 'Auto-VNC session timed out.');
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
                TARGET_URL,
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--start-maximized',
                '--window-size=1920,1080',
                '--disable-dev-shm-usage',
                '--disable-infobars',
                '--no-first-run',
                '--no-default-browser-check',
            ],
            ignoreDefaultArgs: ['--enable-automation'],
        });

        await sleep(600);

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

        await page.setViewport({ width: 1920, height: 1080 });
        await page.bringToFront();

        let tokenCaptured = false;

        // 1. Setup Request Interception across all frames and pages
        async function setupInterception(targetPage) {
            try {
                targetPage.on('request', async (req) => {
                    const url = req.url();
                    const headers = req.headers();
                    const auth = headers['authorization'] || headers['Authorization'];

                    if (url.includes('api2.diskwala.net') || url.includes('/api/diskwala/')) {
                        if (auth && (auth.startsWith('Bearer ') || auth.includes('query_id='))) {
                            if (!tokenCaptured) {
                                tokenCaptured = true;
                                console.log(`[DISKWALA_TOKEN_CAPTURED] ${auth}`);
                                console.log(`[TOKEN_CAPTURED] ${auth}`);
                                logStatus('🎉 Diskwala token captured successfully!');
                            }
                        }
                    }

                    if (url.includes('apiwala.teradownloader.pro') || url.includes('/api/terabox/')) {
                        if (auth && (auth.startsWith('Bearer ') || auth.includes('user='))) {
                            if (!tokenCaptured) {
                                tokenCaptured = true;
                                console.log(`[TERABOX_TOKEN_CAPTURED] ${auth}`);
                                logStatus('🎉 TeraBox token captured successfully!');
                            }
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

        // 2. Navigate to Telegram Web bot chat if not already on it
        if (!page.url().includes('telegram.org')) {
            logStatus(`Navigating to Telegram Web: ${TARGET_URL}...`);
            try {
                await page.goto(TARGET_URL, { waitUntil: 'domcontentloaded', timeout: 50000 });
            } catch (e) {
                logStatus(`Navigation notice: ${e.message}. Continuing...`);
            }
        }
        await page.bringToFront();

        // 3. Check for login state (QR code or phone entry)
        logStatus('Checking Telegram Web login state...');
        await sleep(4000);

        const isLoginPage = await page.evaluate(() => {
            const hasQr = document.querySelector('.qr-code, canvas, .qr-container, .auth-qr');
            const hasPhoneInput = document.querySelector('input[type="tel"], .input-field-input, #sign-in-phone-number');
            const hasLoginTitle = Array.from(document.querySelectorAll('h1, h2, h3, h4, div')).some(el => {
                const t = (el.innerText || '').toLowerCase();
                return t.includes('log in to telegram') || t.includes('scan from mobile');
            });
            return Boolean((hasQr || hasPhoneInput) && hasLoginTitle);
        });

        if (isLoginPage) {
            console.log('[AUTOVNC_LOGIN_REQUIRED] Telegram Web is not logged in! Please run /vnc to log in.');
            await cleanupAndExit(1, 'AUTOVNC_ERROR', 'Telegram Web session is logged out. Login required via /vnc.');
            return;
        }

        // 4. Wait for Telegram Web chat interface to load
        logStatus('Waiting for chat interface to render...');
        let chatLoaded = false;
        for (let i = 0; i < 25; i++) {
            chatLoaded = await page.evaluate(() => {
                return Boolean(
                    document.querySelector('.chat, .messages-container, .bubbles, .middle-column, .chat-input, .MessageList')
                );
            });
            if (chatLoaded) break;
            await sleep(1500);
        }

        if (!chatLoaded) {
            logStatus('Chat container did not render in expected time. Checking if bot needs to be opened...');
        }

        await sleep(2000);

        // 5. Check if "START" button is present (for new or restarted bots)
        try {
            const startClicked = await page.evaluate(() => {
                const buttons = Array.from(document.querySelectorAll('button, .btn-primary, .chat-input-control'));
                const startBtn = buttons.find(b => {
                    const t = (b.innerText || '').trim().toUpperCase();
                    return t === 'START' || t === 'RESTART' || t === '/START';
                });
                if (startBtn) {
                    startBtn.click();
                    return true;
                }
                return false;
            });
            if (startClicked) {
                logStatus('Clicked START button. Waiting for bot response...');
                await sleep(4000);
            }
        } catch (_) {}

        // 6. Look for MiniApp trigger buttons
        logStatus('Searching for MiniApp launch button...');
        let appTriggerClicked = false;

        // Try up to 30 seconds to find and click the MiniApp button
        for (let attempt = 0; attempt < 15; attempt++) {
            if (tokenCaptured) break;

            appTriggerClicked = await page.evaluate((plat) => {
                // Priority A: Search inline keyboard buttons in messages
                const inlineButtons = Array.from(document.querySelectorAll(
                    '.reply-markup button, .inline-button, .InlineButton, button.Button, .reply-keyboard button'
                ));

                for (const btn of inlineButtons) {
                    const text = (btn.innerText || '').toLowerCase().trim();
                    if (
                        text.includes('open') ||
                        text.includes('app') ||
                        text.includes('download') ||
                        text.includes('mini') ||
                        text.includes(plat) ||
                        text.includes('start')
                    ) {
                        btn.scrollIntoView({ block: 'center' });
                        btn.click();
                        return true;
                    }
                }

                // Priority B: Search bottom bar web-app / bot-menu button
                const bottomButtons = Array.from(document.querySelectorAll(
                    '.bot-menu-button, button.is-web-app, .chat-secondary-action, .bot-menu, .btn-primary, button[title*="menu" i]'
                ));

                for (const btn of bottomButtons) {
                    const text = (btn.innerText || '').toLowerCase().trim();
                    if (text.includes('open') || text.includes('app') || text.includes('menu') || btn.classList.contains('bot-menu-button')) {
                        btn.scrollIntoView({ block: 'center' });
                        btn.click();
                        return true;
                    }
                }

                // Priority C: Any visible button containing "Open" or "App"
                const allButtons = Array.from(document.querySelectorAll('button, div[role="button"], a[role="button"]'));
                const candidate = allButtons.find(b => {
                    const t = (b.innerText || '').toLowerCase().trim();
                    return (t.includes('open app') || t.includes('open diskwala') || t.includes('open terabox') || t === 'open');
                });

                if (candidate) {
                    candidate.scrollIntoView({ block: 'center' });
                    candidate.click();
                    return true;
                }

                return false;
            }, PLATFORM);

            if (appTriggerClicked) {
                logStatus('MiniApp trigger button clicked! Checking for confirmation modal...');
                break;
            }

            await sleep(2000);
        }

        await sleep(2500);

        // 7. Handle Telegram Web's confirmation modal ("Open this web app?", "Launch", "Confirm")
        try {
            await page.evaluate(() => {
                const modalButtons = Array.from(document.querySelectorAll(
                    '.modal-dialog button, .popup-button, .confirm-dialog-button, .Button.confirm, button.danger, button.primary, .modal button'
                ));
                const confirmBtn = modalButtons.find(b => {
                    const t = (b.innerText || '').toLowerCase().trim();
                    return (
                        t.includes('launch') ||
                        t.includes('open') ||
                        t.includes('confirm') ||
                        t.includes('continue') ||
                        t.includes('proceed') ||
                        t === 'yes'
                    );
                });
                if (confirmBtn) {
                    confirmBtn.click();
                }
            });
        } catch (_) {}

        // 8. Locate the MiniApp iframe
        logStatus('Locating MiniApp iframe...');
        let targetFrame = null;
        const frameWaitStart = Date.now();

        while ((Date.now() - frameWaitStart) < 45000) {
            if (tokenCaptured) break;

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
            await sleep(2000);
        }

        if (!targetFrame) {
            logStatus('Primary iframe not isolated; scanning all frames directly...');
            targetFrame = page.mainFrame();
        } else {
            logStatus(`MiniApp iframe located: ${targetFrame.url().slice(0, 60)}...`);
        }

        await sleep(3000);

        // 9. Enter dummy link into input field
        const dummyUrl = DUMMY_LINKS[PLATFORM] || DUMMY_LINKS['diskwala'];
        logStatus(`Entering dummy link for ${PLATFORM}...`);

        let inputFoundAndFilled = false;
        const allFramesToTry = [targetFrame, ...page.frames().filter(f => f !== targetFrame)];

        for (const frame of allFramesToTry) {
            if (inputFoundAndFilled || tokenCaptured) break;

            try {
                // Find input field
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
                    // Focus & Clear
                    await inputHandle.click();
                    await sleep(300);

                    await frame.evaluate((el) => {
                        el.value = '';
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }, inputHandle);

                    // Human-like typing
                    for (const char of dummyUrl) {
                        await inputHandle.type(char, { delay: Math.floor(Math.random() * 35) + 25 });
                    }
                    await sleep(600);

                    // Click Download button beside the input
                    logStatus('Locating and clicking Download button...');
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
                            btn.scrollIntoView({ block: 'center' });
                            btn.click();
                            return true;
                        }

                        // Try button nearest to input
                        const inputEl = document.querySelector('input');
                        if (inputEl && inputEl.parentElement) {
                            const siblingBtn = inputEl.parentElement.querySelector('button');
                            if (siblingBtn) {
                                siblingBtn.click();
                                return true;
                            }
                        }
                        return false;
                    });

                    if (clicked) {
                        inputFoundAndFilled = true;
                        logStatus('Download button clicked! Awaiting API token interception...');
                    }
                }
            } catch (_) {}
        }

        // 10. Wait up to 30 seconds for the token to be intercepted
        const captureWaitStart = Date.now();
        while (!tokenCaptured && (Date.now() - captureWaitStart) < 30000) {
            await sleep(1000);
        }

        if (tokenCaptured) {
            await sleep(1500);
            await cleanupAndExit(0, 'AUTOVNC_SUCCESS', 'Token captured and verified successfully!');
        } else {
            await cleanupAndExit(1, 'AUTOVNC_ERROR', 'MiniApp was triggered but bearer token was not emitted.');
        }

    } catch (err) {
        logStatus(`Unhandled AutoVNC error: ${err.message}`);
        await cleanupAndExit(1, 'AUTOVNC_ERROR', err.message);
    }
})();
