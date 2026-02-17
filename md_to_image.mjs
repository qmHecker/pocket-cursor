import puppeteer from 'puppeteer';
import { marked } from 'marked';
import fs from 'fs/promises';
import path from 'path';

/**
 * md_to_image.mjs
 * Render a Markdown file to a styled PNG image.
 *
 * Usage:
 *   node md_to_image.mjs input.md                  # outputs input.png next to input.md
 *   node md_to_image.mjs input.md --out photo.png  # explicit output path
 *
 * Used by PocketCursor's phone outbox to render .md files as styled
 * images before sending them to Telegram.
 */

const args = process.argv.slice(2);
if (args.length === 0) {
    console.error('Usage: node md_to_image.mjs <input.md> [--out output.png] [--width 450]');
    process.exit(1);
}

const inputPath = path.resolve(args[0]);
const outIdx = args.indexOf('--out');
const outputPath = outIdx !== -1 && args[outIdx + 1]
    ? path.resolve(args[outIdx + 1])
    : inputPath.replace(/\.md$/i, '.png');
const widthIdx = args.indexOf('--width');
const viewportWidth = widthIdx !== -1 && args[widthIdx + 1]
    ? parseInt(args[widthIdx + 1], 10)
    : 450;

// Read the markdown file
const reportDir = path.dirname(inputPath);
let markdownContent = await fs.readFile(inputPath, 'utf-8');

// Convert markdown to HTML
let htmlContent = marked(markdownContent);

// Function to convert image to base64
async function imageToBase64(imagePath) {
    const imageBuffer = await fs.readFile(imagePath);
    const base64 = imageBuffer.toString('base64');
    const ext = path.extname(imagePath).toLowerCase();
    const mimeType = ext === '.png' ? 'image/png' : ext === '.jpg' || ext === '.jpeg' ? 'image/jpeg' : 'image/gif';
    return `data:${mimeType};base64,${base64}`;
}

// Find all image references and convert to base64
const imgRegex = /src="([^"]+)"/g;
let match;
const replacements = [];

while ((match = imgRegex.exec(htmlContent)) !== null) {
    const originalSrc = match[1];
    let imagePath;
    
    if (originalSrc.startsWith('../')) {
        imagePath = path.resolve(reportDir, originalSrc);
    } else if (!originalSrc.startsWith('http') && !originalSrc.startsWith('data:')) {
        imagePath = path.resolve(reportDir, originalSrc);
    }
    
    if (imagePath) {
        try {
            const base64Src = await imageToBase64(imagePath);
            replacements.push({ original: originalSrc, replacement: base64Src });
        } catch (error) {
            // Image not found, skip
        }
    }
}

// Apply all replacements
for (const { original, replacement } of replacements) {
    htmlContent = htmlContent.replace(`src="${original}"`, `src="${replacement}"`);
}

// Clean HTML based on test template (Montserrat, 0.04em spacing, 1.4 line-height)
const fullHtml = `
<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="utf-8">
    <title>PocketCursor Image</title>
    <link href="https://fonts.googleapis.com/css2?family=Montserrat:ital,wght@0,400;0,600;1,400;1,600&display=swap" rel="stylesheet">
    <style>
        /* ===== PAGE SETUP ===== */
        @page {
            size: A4 portrait;
            margin: 15mm 20mm 25mm 20mm;
        }

        /* ===== BODY & TYPOGRAPHY ===== */
        body {
            font-family: 'Montserrat', sans-serif;
            font-size: 10pt;
            line-height: 1.6;
            letter-spacing: 0.04em;
            color: #333;
            text-align: justify;
            margin: 0;
            padding: 20px;
        }
        
        /* Remove any space before first element */
        body > *:first-child {
            margin-top: 0 !important;
            padding-top: 0 !important;
        }

        /* ===== HEADERS ===== */
        h1, h2, h3, h4, h5, h6 {
            font-weight: 600;
            color: #2c3e50;
            letter-spacing: 0em;
            text-align: left;
            page-break-after: avoid;
        }

        h1 {
            font-size: 14pt;
            margin-top: 0;
            margin-bottom: 0pt;
            line-height: 1.2;
        }

        h2 {
            font-size: 13pt;
            margin-bottom: 3pt;
            color: #34495e;
            page-break-before: auto;
        }
        
        /* First h2 should not break from h1 */
        h1 + h2 {
            page-break-before: avoid;
            margin-top: 0;
        }

        h3 {
            font-size: 12pt;
            line-height: 1.0;
            margin-top: 12pt;
            margin-bottom: 0pt;
            color: #34495e;
            page-break-after: avoid;
        }
    
        /* ===== PARAGRAPHS ===== */
        p {
            margin-bottom: 4pt;
            text-align: justify;
            orphans: 2;
            widows: 2;
        }

        strong {
            font-weight: 600;
            color: #2c3e50;
        }

        em {
            font-style: italic;
        }

        /* ===== BLOCKQUOTES ===== */
        blockquote {
            margin: 10pt 0;
            padding: 8pt 12pt;
            background-color: #f8f9fa;
            border-left: 3pt solid #6c757d;
            font-size: 10pt;
            line-height: 1.4;
            color: #555;
        }

        blockquote p {
            margin: 0;
        }

        /* ===== CODE ===== */
        code {
            font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
            font-size: 9pt;
            background-color: #f4f4f4;
            padding: 1pt 4pt;
            border-radius: 3pt;
            letter-spacing: 0;
            white-space: nowrap;
        }

        pre {
            font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
            font-size: 8.5pt;
            background-color: #f8f9fa;
            border: 1pt solid #e9ecef;
            border-radius: 4pt;
            padding: 10pt 12pt;
            margin: 12pt 0;
            overflow-x: auto;
            line-height: 1.4;
            letter-spacing: 0;
            page-break-inside: avoid;
        }

        pre code {
            background-color: transparent;
            padding: 0;
            border-radius: 0;
            font-size: inherit;
            white-space: pre;
        }

        /* ===== LISTS ===== */
        ul, ol {
            margin: 10pt 0;
            padding-left: 20pt;
        }

        li {
            margin-bottom: 4pt;
            line-height: 1.3;
        }

        ul ul, ol ol, ul ol, ol ul {
            margin-top: 6pt;
            margin-bottom: 6pt;
        }

        /* ===== TABLES ===== */
        table {
            width: 100%;
            border-collapse: collapse;
            margin: 16pt 0;
            font-size: 10pt;
            letter-spacing: 0.02em;
            page-break-inside: auto;
        }

        th {
            background-color: #2c3e50;
            color: white;
            font-weight: 600;
            text-align: left;
            padding: 6pt 8pt;
            border-bottom: 2pt solid #2c3e50;
        }
        
        /* Empty header rows: make thin border line instead of thick bar */
        th:empty {
            padding: 0;
            height: 2pt;
            background-color: #2c3e50;
            border: none;
        }

        td {
            padding: 4pt 8pt;
            border-bottom: 1pt solid #e9ecef;
        }
        
        /* Prevent awkward line breaks in table cells */
        td:last-child {
            white-space: nowrap;
        }

        /* No alternating row colors */

        tr:last-child td {
            border-bottom: 2pt solid #2c3e50;
        }

        /* ===== IMAGES ===== */
        img {
            max-width: 100%;
            height: auto;
            display: block;
            margin: 12pt 0;
            page-break-inside: avoid;
        }

        /* ===== HORIZONTAL RULES ===== */
        hr {
            border: none;
            border-top: none;
            margin: 16pt 0;
            height: 0;
        }

        /* ===== PAGE BREAKS ===== */
        .page-break {
            page-break-after: always;
        }
    </style>
</head>
<body>
    ${htmlContent}
</body>
</html>
`;

// Render to PNG
const browser = await puppeteer.launch({ 
    headless: true,
    args: ['--no-sandbox', '--disable-setuid-sandbox']
});

const page = await browser.newPage();

// Width determines aspect ratio; height adjusts to content via fullPage screenshot
await page.setViewport({ width: viewportWidth, height: 600, deviceScaleFactor: 2 });

await page.setContent(fullHtml, { 
    waitUntil: 'networkidle0' 
});

await page.screenshot({
    path: outputPath,
    fullPage: true,
});

await browser.close();

const stats = await fs.stat(outputPath);
console.log(`${path.basename(inputPath)} -> ${path.basename(outputPath)} (${(stats.size / 1024).toFixed(1)} KB)`);
