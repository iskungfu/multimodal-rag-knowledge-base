/* Safe Markdown renderer — builds DOM nodes, never injects raw HTML.
   Supports: headings, paragraphs, bold/italic/strike, inline code,
   fenced code blocks, lists (nested + task), tables, blockquotes,
   links, images, horizontal rules. */

import {h, icon, copyToClipboard, toast} from './ui.js';

const LIST_RE = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
const HEADING_RE = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const FENCE_RE = /^\s{0,3}(`{3,}|~{3,})\s*([\w+#.-]*)\s*$/;
const HR_RE = /^\s{0,3}((\*\s*){3,}|(-\s*){3,}|(_\s*){3,})$/;
const BLOCKQUOTE_RE = /^\s{0,3}>\s?/;
const IMAGE_EXT_RE = /\.(png|jpe?g|gif|webp|bmp|svg)(\?|#|$)/i;

function safeHref(raw) {
    const url = String(raw || '').trim();
    if (!url) return '';
    if (/^(https?:\/\/|mailto:|tel:)/i.test(url)) return url;
    if (/^(\/|\.{1,2}\/|#)/.test(url)) return url;
    return '';
}

function isTableDelimiter(line) {
    const cells = splitTableRow(line);
    return cells.length > 0 && cells.every((cell) => /^:?-{2,}:?$/.test(cell));
}

function splitTableRow(line) {
    let value = String(line || '').trim();
    if (value.startsWith('|')) value = value.slice(1);
    if (value.endsWith('|')) value = value.slice(0, -1);
    return value.split('|').map((cell) => cell.trim().replace(/\\\|/g, '|'));
}

function startsBlock(lines, index) {
    const line = lines[index];
    if (!line || !line.trim()) return true;
    if (HEADING_RE.test(line)) return true;
    if (FENCE_RE.test(line)) return true;
    if (HR_RE.test(line)) return true;
    if (BLOCKQUOTE_RE.test(line)) return true;
    if (LIST_RE.test(line)) return true;
    if (line.includes('|') && index + 1 < lines.length && isTableDelimiter(lines[index + 1])) {
        return true;
    }
    return false;
}

function matchLink(text, start) {
    // text[start] === '['
    let i = start + 1;
    let depth = 1;
    let label = '';

    while (i < text.length) {
        const ch = text[i];
        if (ch === '\\' && i + 1 < text.length) {
            label += text[i + 1];
            i += 2;
            continue;
        }
        if (ch === '[') depth += 1;
        if (ch === ']') {
            depth -= 1;
            if (depth === 0) break;
        }
        label += ch;
        i += 1;
    }

    if (depth !== 0 || text[i + 1] !== '(') return null;

    let j = i + 2;
    let url = '';
    let paren = 1;
    while (j < text.length) {
        const ch = text[j];
        if (ch === '(') paren += 1;
        if (ch === ')') {
            paren -= 1;
            if (paren === 0) break;
        }
        url += ch;
        j += 1;
    }
    if (paren !== 0) return null;

    return {
        label,
        href: url.trim().replace(/^<|>$/g, ''),
        end: j + 1,
    };
}

function buildCodeBlock(code, lang) {
    const copyBtn = h(
        'button',
        {
            class: 'code-copy',
            type: 'button',
            'aria-label': '复制代码',
        },
        icon('copy', {size: 13}),
        h('span', {text: '复制'})
    );

    copyBtn.addEventListener('click', async () => {
        const ok = await copyToClipboard(code);
        if (!ok) {
            toast('复制失败，请手动选择代码', {type: 'error'});
            return;
        }
        copyBtn.replaceChildren(icon('check', {size: 13}), h('span', {text: '已复制'}));
        copyBtn.classList.add('is-copied');
        setTimeout(() => {
            copyBtn.replaceChildren(icon('copy', {size: 13}), h('span', {text: '复制'}));
            copyBtn.classList.remove('is-copied');
        }, 1600);
    });

    return h(
        'div',
        {class: 'code-block'},
        h(
            'div',
            {class: 'code-head'},
            h('span', {class: 'code-lang', text: lang || 'code'}),
            copyBtn
        ),
        h('pre', {}, h('code', {class: lang ? `language-${lang}` : '', text: code}))
    );
}

function buildImage(alt, rawSrc) {
    const src = safeHref(rawSrc);
    if (!src || !IMAGE_EXT_RE.test(src)) {
        return h('span', {text: alt || rawSrc || ''});
    }
    return h(
        'span',
        {class: 'md-image'},
        h('img', {
            src,
            alt: alt || '图片',
            loading: 'lazy',
            decoding: 'async',
            referrerpolicy: 'no-referrer',
            onerror: (event) => {
                event.currentTarget.closest('.md-image')?.remove();
            },
        })
    );
}

function appendInline(parent, text) {
    let i = 0;
    let buffer = '';

    const flush = () => {
        if (buffer) {
            parent.appendChild(document.createTextNode(buffer));
            buffer = '';
        }
    };

    while (i < text.length) {
        const ch = text[i];

        // Escaped character
        if (ch === '\\' && i + 1 < text.length && /[\\`*_{}[\]()#+\-.!~>|]/.test(text[i + 1])) {
            buffer += text[i + 1];
            i += 2;
            continue;
        }

        // Inline code
        if (ch === '`') {
            const ticks = (text.slice(i).match(/^`+/) || ['`'])[0];
            const end = text.indexOf(ticks, i + ticks.length);
            if (end !== -1) {
                flush();
                parent.appendChild(h('code', {text: text.slice(i + ticks.length, end)}));
                i = end + ticks.length;
                continue;
            }
            buffer += ticks;
            i += ticks.length;
            continue;
        }

        // Image
        if (ch === '!' && text[i + 1] === '[') {
            const link = matchLink(text, i + 1);
            if (link) {
                flush();
                parent.appendChild(buildImage(link.label, link.href));
                i = link.end;
                continue;
            }
        }

        // Link
        if (ch === '[') {
            const link = matchLink(text, i);
            if (link) {
                flush();
                const href = safeHref(link.href);
                if (href) {
                    const anchor = h('a', {
                        href,
                        target: href.startsWith('#') ? null : '_blank',
                        rel: 'noopener noreferrer',
                    });
                    appendInline(anchor, link.label);
                    parent.appendChild(anchor);
                } else {
                    appendInline(parent, link.label);
                }
                i = link.end;
                continue;
            }
        }

        // Bold
        if (ch === '*' && text[i + 1] === '*') {
            const end = text.indexOf('**', i + 2);
            if (end > i + 2) {
                flush();
                const strong = h('strong');
                appendInline(strong, text.slice(i + 2, end));
                parent.appendChild(strong);
                i = end + 2;
                continue;
            }
        }
        if (ch === '_' && text[i + 1] === '_') {
            const end = text.indexOf('__', i + 2);
            if (end > i + 2) {
                flush();
                const strong = h('strong');
                appendInline(strong, text.slice(i + 2, end));
                parent.appendChild(strong);
                i = end + 2;
                continue;
            }
        }

        // Strikethrough
        if (ch === '~' && text[i + 1] === '~') {
            const end = text.indexOf('~~', i + 2);
            if (end > i + 2) {
                flush();
                const del = h('del');
                appendInline(del, text.slice(i + 2, end));
                parent.appendChild(del);
                i = end + 2;
                continue;
            }
        }

        // Italic (single marker, not intraword underscore)
        if (ch === '*' || ch === '_') {
            const prev = i > 0 ? text[i - 1] : '';
            const isIntraword = ch === '_' && /[\w\u4e00-\u9fa5]/.test(prev);
            const next = text[i + 1] || '';
            if (!isIntraword && next && !/\s/.test(next)) {
                const end = text.indexOf(ch, i + 1);
                if (end > i + 1 && !/\s/.test(text[end - 1])) {
                    flush();
                    const em = h('em');
                    appendInline(em, text.slice(i + 1, end));
                    parent.appendChild(em);
                    i = end + 1;
                    continue;
                }
            }
        }

        // Autolink
        if (ch === 'h' || ch === 'H') {
            const match = text.slice(i).match(/^https?:\/\/[^\s<>"'）】。，；]+/i);
            if (match) {
                let url = match[0];
                while (/[.,;:!?)\]}]$/.test(url)) url = url.slice(0, -1);
                if (url.length > 8) {
                    flush();
                    parent.appendChild(
                        h('a', {
                            href: url,
                            target: '_blank',
                            rel: 'noopener noreferrer',
                            text: url,
                        })
                    );
                    i += url.length;
                    continue;
                }
            }
        }

        buffer += ch;
        i += 1;
    }

    flush();
}

function appendListItem(li, text) {
    const task = text.match(/^\[([ xX])\]\s+(.*)$/);
    if (!task) {
        appendInline(li, text);
        return;
    }

    const checked = task[1].trim().toLowerCase() === 'x';
    li.classList.add('task-item');
    li.appendChild(
        h(
            'span',
            {class: `task-box${checked ? ' is-checked' : ''}`, 'aria-hidden': 'true'},
            checked ? icon('check', {size: 11, strokeWidth: 2.6}) : null
        )
    );
    appendInline(li, task[2]);
}

function parseList(lines, start) {
    const first = lines[start].match(LIST_RE);
    const ordered = /\d/.test(first[2]);
    const baseIndent = first[1].replace(/\t/g, '    ').length;
    const list = h(ordered ? 'ol' : 'ul');
    let currentItem = null;
    let i = start;

    while (i < lines.length) {
        const line = lines[i];

        if (!line.trim()) {
            const next = lines[i + 1];
            if (next && LIST_RE.test(next)) {
                i += 1;
                continue;
            }
            break;
        }

        const match = line.match(LIST_RE);
        if (match) {
            const indent = match[1].replace(/\t/g, '    ').length;
            const itemOrdered = /\d/.test(match[2]);
            if (indent < baseIndent || itemOrdered !== ordered) break;

            if (indent > baseIndent) {
                const nested = parseList(lines, i);
                if (currentItem) currentItem.appendChild(nested.el);
                i = nested.next;
                continue;
            }

            currentItem = h('li');
            appendListItem(currentItem, match[3]);
            list.appendChild(currentItem);
            i += 1;
            continue;
        }

        const indent = line.match(/^\s*/)[0].replace(/\t/g, '    ').length;
        if (currentItem && indent > baseIndent) {
            currentItem.appendChild(h('br'));
            appendInline(currentItem, line.trim());
            i += 1;
            continue;
        }

        break;
    }

    return {el: list, next: i};
}

function parseTable(lines, start) {
    const headerCells = splitTableRow(lines[start]);
    const alignment = splitTableRow(lines[start + 1]).map((cell) => {
        const left = cell.startsWith(':');
        const right = cell.endsWith(':');
        if (left && right) return 'center';
        if (right) return 'right';
        return 'left';
    });

    const thead = h('thead');
    const headRow = h('tr');
    headerCells.forEach((cell, index) => {
        const th = h('th', {style: {textAlign: alignment[index] || 'left'}});
        appendInline(th, cell);
        headRow.appendChild(th);
    });
    thead.appendChild(headRow);

    const tbody = h('tbody');
    let i = start + 2;
    while (i < lines.length && lines[i].trim() && lines[i].includes('|')) {
        if (isTableDelimiter(lines[i])) break;
        const row = h('tr');
        const cells = splitTableRow(lines[i]);
        for (let index = 0; index < headerCells.length; index += 1) {
            const td = h('td', {style: {textAlign: alignment[index] || 'left'}});
            appendInline(td, cells[index] ?? '');
            row.appendChild(td);
        }
        tbody.appendChild(row);
        i += 1;
    }

    const table = h('table', {}, thead, tbody);
    return {el: h('div', {class: 'table-wrap'}, table), next: i};
}

/**
 * Render Markdown into a DocumentFragment.
 * @param {string} source
 * @returns {DocumentFragment}
 */
export function renderMarkdown(source) {
    const fragment = document.createDocumentFragment();
    const lines = String(source ?? '').replace(/\r\n?/g, '\n').split('\n');
    let i = 0;

    while (i < lines.length) {
        const line = lines[i];

        if (!line.trim()) {
            i += 1;
            continue;
        }

        // Fenced code block
        const fence = line.match(FENCE_RE);
        if (fence) {
            const marker = fence[1][0];
            const markerLength = fence[1].length;
            const lang = fence[2] || '';
            const codeLines = [];
            i += 1;
            while (i < lines.length) {
                const closing = lines[i].match(/^\s{0,3}(`{3,}|~{3,})\s*$/);
                if (closing && closing[1][0] === marker && closing[1].length >= markerLength) {
                    i += 1;
                    break;
                }
                codeLines.push(lines[i]);
                i += 1;
            }
            fragment.appendChild(buildCodeBlock(codeLines.join('\n'), lang));
            continue;
        }

        // Heading (# → h2, keeps a single h1 on the page)
        const heading = line.match(HEADING_RE);
        if (heading) {
            const level = Math.min(heading[1].length + 1, 6);
            const node = h(`h${level}`);
            appendInline(node, heading[2]);
            fragment.appendChild(node);
            i += 1;
            continue;
        }

        // Horizontal rule
        if (HR_RE.test(line)) {
            fragment.appendChild(h('hr'));
            i += 1;
            continue;
        }

        // Blockquote
        if (BLOCKQUOTE_RE.test(line)) {
            const quoteLines = [];
            while (i < lines.length && BLOCKQUOTE_RE.test(lines[i])) {
                quoteLines.push(lines[i].replace(BLOCKQUOTE_RE, ''));
                i += 1;
            }
            const quote = h('blockquote');
            quote.appendChild(renderMarkdown(quoteLines.join('\n')));
            fragment.appendChild(quote);
            continue;
        }

        // Table
        if (line.includes('|') && i + 1 < lines.length && isTableDelimiter(lines[i + 1])) {
            const table = parseTable(lines, i);
            fragment.appendChild(table.el);
            i = table.next;
            continue;
        }

        // List
        if (LIST_RE.test(line)) {
            const list = parseList(lines, i);
            fragment.appendChild(list.el);
            i = list.next;
            continue;
        }

        // Paragraph
        const paragraph = h('p');
        let firstLine = true;
        while (i < lines.length && lines[i].trim() && !startsBlock(lines, i)) {
            if (!firstLine) paragraph.appendChild(h('br'));
            appendInline(paragraph, lines[i].trim());
            firstLine = false;
            i += 1;
        }
        if (firstLine) {
            // Safety valve: never loop forever on an unrecognized block.
            appendInline(paragraph, lines[i].trim());
            i += 1;
        }
        fragment.appendChild(paragraph);
    }

    return fragment;
}

/**
 * Replace an element's content with rendered Markdown.
 * @param {HTMLElement} el
 * @param {string} source
 */
export function renderMarkdownInto(el, source) {
    el.replaceChildren(renderMarkdown(source));
}
