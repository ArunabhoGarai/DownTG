/**
 * TeraBox Puppeteer Stealth Xvfb Crawler Worker
 * =============================================
 * Invoked by Python Telegram Bot (terabox_downloader.py).
 * 
 * Usage:
 *    node terabox_crawler.js <TERABOX_URL> <OUTPUT_DIR> [DOWNLOAD_ID]
 *
 * Output:
 *    Streams real-time progress lines to stdout (e.g., "[STATUS] ...", "[PROGRESS] 45%").
 *    Emits final JSON on stdout upon completion:
 *    {"success": true, "filepath": "...", "title": "...", "filesize": 12345, "thumbnail": "..."}
 */

const fs = require('fs');
const path = require('path');

const TARGET_URL = process.argv[2];
const OUTPUT_DIR = process.argv[3] || path.join(__dirname, 'downloads');
const DOWNLOAD_ID = process.argv[4] || Date.now().toString();

if (!TARGET_URL) {
    console.log(JSON.stringify({ success: false, error: 'Target TeraBox URL is required.' }));
    process.exit(1);
}

// Ensure output directory exists
if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}

const COOKIE_FILE = path.join(__dirname, 'cooky', 'terabox', 'cookies.txt');

function parseNetscapeCookies(filePath) {
    if (!fs.existsSync(filePath)) {
        return [];
    }
    try {
        const content = fs.readFileSync(filePath, 'utf8');
        const cookies = [];
        content.split('\n').forEach(line => {
            line = line.trim();
            if (!line || line.startsWith('#')) return;
            const parts = line.split('\t');
            if (parts.length >= 7) {
                cookies.push({
                    name: parts[5],
                    value: parts[6],
                    domain: parts[0],
                    path: parts[2],
                    secure: parts[3].toLowerCase() === 'true',
                });
            }
        });
        return cookies;
    } catch (e) {
        return [];
    }
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
            const puppeteerModule = await import('puppeteer');
            puppeteer = puppeteerModule.default || puppeteerModule;
        } catch (e) {
            console.log(JSON.stringify({ success: false, error: 'Puppeteer is not installed in node_modules.' }));
            process.exit(1);
        }
    }

    logStatus('Launching browser with stealth mode (1920x1080 Full HD)...');
    const isLinux = process.platform === 'linux';
    const launchArgs = [
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--disable-dev-shm-usage',
        '--disable-blink-features=AutomationControlled',
        '--window-size=1920,1080',
        '--start-maximized',
    ];

    if (isLinux) {
        launchArgs.push('--disable-gpu');
    }

    let browser;
    try {
        browser = await puppeteer.launch({
            headless: false, // GUI mode under Xvfb / Desktop
            defaultViewport: { width: 1920, height: 1080 },
            args: launchArgs,
        });
    } catch (launchErr) {
        // Fallback to headless: "new" if no display server is running
        try {
            browser = await puppeteer.launch({
                headless: 'new',
                defaultViewport: { width: 1920, height: 1080 },
                args: launchArgs,
            });
        } catch (fbErr) {
            console.log(JSON.stringify({ success: false, error: `Browser launch failed: ${launchErr.message}` }));
            process.exit(1);
        }
    }

    const page = await browser.newPage();
    await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36');
    await page.setViewport({ width: 1920, height: 1080, deviceScaleFactor: 1 });

    // 1. Inject Session Cookies
    const cookies = parseNetscapeCookies(COOKIE_FILE);
    if (cookies.length > 0) {
        logStatus(`Injecting ${cookies.length} session cookies...`);
        try {
            await page.setCookie(...cookies);
        } catch (cookieErr) {}
    }

    // 2. Configure native download directory via CDP
    const downloadDir = path.resolve(OUTPUT_DIR);
    const client = await page.target().createCDPSession();
    try {
        await client.send('Browser.setDownloadBehavior', {
            behavior: 'allow',
            downloadPath: downloadDir,
            eventsEnabled: true,
        });
        await client.send('Page.setDownloadBehavior', {
            behavior: 'allow',
            downloadPath: downloadDir,
        });
    } catch (cdpErr) {
        logStatus(`CDP DownloadBehavior warning: ${cdpErr.message}`);
    }

    let downloadStarted = false;
    let suggestedFilename = null;

    client.on('Browser.downloadWillBegin', (event) => {
        downloadStarted = true;
        suggestedFilename = event.suggestedFilename || `tera_${DOWNLOAD_ID}.mp4`;
        logStatus(`Download initiated by browser: ${suggestedFilename}`);
    });

    // 3. Navigate to TeraBox share URL
    logStatus(`Navigating to TeraBox link...`);
    try {
        await page.goto(TARGET_URL, { waitUntil: 'networkidle2', timeout: 40000 });
    } catch (navErr) {
        // Continue even if networkidle2 times out
    }

    logStatus('Adjusting page zoom & waiting for components to render...');
    try {
        await page.evaluate(() => {
            // Zoom out slightly to unveil all bottom buttons
            document.body.style.zoom = '85%';
            window.scrollBy(0, 250);
        });
    } catch (e) {}

    await new Promise(r => setTimeout(r, 3500));

    // 4. Extract page metadata
    let pageMetadata = { title: `TeraBox_Video_${DOWNLOAD_ID}`, thumbnail: null };
    try {
        pageMetadata = await page.evaluate((fallbackTitle) => {
            const rawTitle = document.title || '';
            const cleanTitle = rawTitle.replace(' - Share Files Online & Send Larges Files with TeraBox', '').trim();
            const videoEl = document.querySelector('video');
            const poster = videoEl ? videoEl.poster : null;
            return {
                title: cleanTitle || fallbackTitle,
                thumbnail: poster || null
            };
        }, `TeraBox_Video_${DOWNLOAD_ID}`);
    } catch (metaErr) {}

    // 5. Dismiss any blocking dialogs/modals
    try {
        await page.evaluate(() => {
            const closeSelectors = [
                '.close-btn', '.modal-close', '.dialog-close', 
                '.el-dialog__headerbtn', 'button[aria-label="Close"]',
                '.tip-modal .close', '.guide-modal-close'
            ];
            for (const sel of closeSelectors) {
                const btn = document.querySelector(sel);
                if (btn) btn.click();
            }
        });
    } catch (e) {}

    // Save screenshot before clicking for debugging
    try {
        await page.screenshot({ path: path.join(downloadDir, 'crawler_before_click.png') });
    } catch (ssErr) {}

    // 6. Find and Click the Download Button
    logStatus('Locating download button (.operate-row button.download-btn)...');
    const candidateSelectors = [
        '.operate-row button.download-btn',
        'button.download-btn',
        '.operate-row .download-btn',
        '.video-oprate-row button.download-btn',
        'button.download-btn span',
        '.btn-download',
        'button[class*="download"]',
    ];

    let buttonHandle = null;
    for (const sel of candidateSelectors) {
        try {
            const el = await page.$(sel);
            if (el) {
                const isVisible = await page.evaluate(e => {
                    const rect = e.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0 && window.getComputedStyle(e).visibility !== 'hidden';
                }, el);
                if (isVisible) {
                    buttonHandle = el;
                    break;
                }
            }
        } catch (e) {}
    }

    if (!buttonHandle) {
        const elements = await page.$$('button, div, a, span');
        for (const el of elements) {
            const text = await page.evaluate(e => (e.innerText || '').trim().toLowerCase(), el);
            if (text === 'download' || text === 'download video' || text.startsWith('download')) {
                const isVisible = await page.evaluate(e => {
                    const rect = e.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                }, el);
                if (isVisible) {
                    buttonHandle = el;
                    break;
                }
            }
        }
    }

    if (buttonHandle) {
        logStatus('Scrolling button into view and clicking...');
        await page.evaluate(el => el.scrollIntoView({ block: 'center', inline: 'center' }), buttonHandle);
        await new Promise(r => setTimeout(r, 600));

        const rect = await buttonHandle.boundingBox();
        if (rect) {
            const clickX = rect.x + rect.width / 2;
            const clickY = rect.y + rect.height / 2;
            await page.mouse.move(clickX, clickY, { steps: 15 });
            await new Promise(r => setTimeout(r, 250));
            await page.mouse.down();
            await new Promise(r => setTimeout(r, 120));
            await page.mouse.up();
        } else {
            await page.evaluate(el => el.click(), buttonHandle);
        }

        await page.evaluate(el => {
            el.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
            el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
            el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
            el.click();
        }, buttonHandle);

        logStatus('Download button clicked successfully.');

        // Check if a modal popped up asking for confirmation or "Download in Browser"
        await new Promise(r => setTimeout(r, 1200));
        try {
            await page.evaluate(() => {
                const modalButtons = Array.from(document.querySelectorAll('.modal-btn, .dialog-btn, .btn, button, div, a'));
                const confirmBtn = modalButtons.find(el => {
                    const t = (el.innerText || '').trim().toLowerCase();
                    return t.includes('normal download') || t.includes('download in browser') || t.includes('continue download') || t === 'confirm' || t === 'yes';
                });
                if (confirmBtn) {
                    confirmBtn.click();
                }
            });
        } catch (mErr) {}

        // Save screenshot after clicking
        try {
            await page.screenshot({ path: path.join(downloadDir, 'crawler_after_click.png') });
        } catch (ssErr) {}

    } else {
        logStatus('Warning: Download button not found by selector. Trying page fallback click...');
    }

    // 7. Track the file download in OUTPUT_DIR until finished
    logStatus('Monitoring download directory for file completion...');
    const snapshotBefore = new Set(fs.existsSync(downloadDir) ? fs.readdirSync(downloadDir) : []);
    
    let downloadedFilePath = null;
    const maxWaitSec = 600; // 10 minutes maximum for large files
    const startTime = Date.now();
    let lastSize = -1;
    let sizeStallCount = 0;

    let retriggered = false;
    while ((Date.now() - startTime) < (maxWaitSec * 1000)) {
        await new Promise(r => setTimeout(r, 1000));
        
        if (!fs.existsSync(downloadDir)) continue;
        const currentFiles = fs.readdirSync(downloadDir);
        
        // Find newly created files
        const newFiles = currentFiles.filter(f => !snapshotBefore.has(f));
        
        // If download hasn't started after 7s, retry clicking all download buttons
        if ((Date.now() - startTime) > 7000 && !retriggered && newFiles.length === 0) {
            retriggered = true;
            logStatus('Re-triggering click and checking modal buttons...');
            try {
                await page.evaluate(() => {
                    const candidates = [
                        '.operate-row button.download-btn',
                        'button.download-btn',
                        '.video-oprate-row button.download-btn',
                        '.btn-download',
                        'button[class*="download"]'
                    ];
                    for (const sel of candidates) {
                        const b = document.querySelector(sel);
                        if (b) {
                            b.click();
                            break;
                        }
                    }
                    const modalButtons = Array.from(document.querySelectorAll('.modal-btn, .dialog-btn, .btn, button, div, a'));
                    const confirmBtn = modalButtons.find(el => {
                        const t = (el.innerText || '').trim().toLowerCase();
                        return t.includes('normal download') || t.includes('download in browser') || t.includes('continue download') || t === 'confirm' || t === 'download';
                    });
                    if (confirmBtn) confirmBtn.click();
                });
            } catch (e) {}
        }

        // Look for in-progress Chrome download (.crdownload)
        const crdownloadFile = newFiles.find(f => f.endsWith('.crdownload') || f.includes('.crdownload'));
        
        if (crdownloadFile) {
            const fullCrPath = path.join(downloadDir, crdownloadFile);
            try {
                const stats = fs.statSync(fullCrPath);
                const currentSizeMB = (stats.size / (1024 * 1024)).toFixed(2);
                console.log(`[PROGRESS] Downloading: ${currentSizeMB} MB`);
                
                if (stats.size === lastSize) {
                    sizeStallCount++;
                } else {
                    lastSize = stats.size;
                    sizeStallCount = 0;
                }
            } catch (e) {}
        } else if (newFiles.length > 0) {
            // Check if a completed media file exists
            const completedFile = newFiles.find(f => {
                const ext = path.extname(f).toLowerCase();
                return ['.mp4', '.mkv', '.avi', '.mov', '.mp3', '.webm', '.zip', '.rar'].includes(ext) || !f.includes('.crdownload');
            });
            
            if (completedFile) {
                const fullPath = path.join(downloadDir, completedFile);
                try {
                    const stats = fs.statSync(fullPath);
                    if (stats.size > 0) {
                        downloadedFilePath = fullPath;
                        logStatus(`Download completed: ${completedFile} (${(stats.size / (1024 * 1024)).toFixed(2)} MB)`);
                        break;
                    }
                } catch (e) {}
            }
        }
    }

    // Close browser cleanly
    try {
        await browser.close();
    } catch (e) {}

    // Emit final result JSON
    if (downloadedFilePath && fs.existsSync(downloadedFilePath)) {
        let finalPath = downloadedFilePath;
        const currentExt = path.extname(downloadedFilePath).toLowerCase();
        
        // Ensure valid media extension
        const rawTitle = pageMetadata.title || `tera_${DOWNLOAD_ID}.mp4`;
        const cleanTitle = rawTitle.replace(/[/\\?%*:|"<>]/g, '_').trim();
        const targetExt = path.extname(cleanTitle) || '.mp4';
        
        const cleanFilename = `tera_${DOWNLOAD_ID}_${path.basename(cleanTitle, targetExt)}${targetExt}`;
        const targetPath = path.join(downloadDir, cleanFilename);
        
        try {
            if (finalPath !== targetPath) {
                fs.renameSync(finalPath, targetPath);
                finalPath = targetPath;
            }
        } catch (renameErr) {
            // Keep original path if rename fails
        }

        const stats = fs.statSync(finalPath);
        const result = {
            success: true,
            filepath: finalPath,
            filename: path.basename(finalPath),
            title: pageMetadata.title || path.basename(finalPath),
            thumbnail: pageMetadata.thumbnail,
            filesize: stats.size,
            download_id: DOWNLOAD_ID,
        };
        console.log(JSON.stringify(result));
        process.exit(0);
    } else {
        const errResult = {
            success: false,
            error: 'Download timed out or no file was saved by browser.',
        };
        console.log(JSON.stringify(errResult));
        process.exit(1);
    }
})();
