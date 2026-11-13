/**
 * Copy buttons. A button with data-copy-text copies that text; one inside a code block's wrapper copies the block.
 * Code blocks in docs get their button added here, so pages without JavaScript just show the code.
 */
async function copy(text: string, button: HTMLButtonElement) {
  const label = button.querySelector<HTMLElement>('[data-copy-label]') ?? button;
  const before = label.textContent;
  try {
    await navigator.clipboard.writeText(text);
    label.textContent = 'Copied';
  } catch {
    label.textContent = 'Copy failed';
  }
  button.setAttribute('aria-live', 'polite');
  window.setTimeout(() => (label.textContent = before), 1800);
}

export function enhanceCopyButtons(root: ParentNode = document) {
  root.querySelectorAll<HTMLButtonElement>('button[data-copy-text]').forEach((b) => {
    b.hidden = false;
    b.addEventListener('click', () => copy(b.dataset.copyText ?? '', b));
  });
}

export function addCodeCopyButtons(root: ParentNode = document) {
  root.querySelectorAll<HTMLPreElement>('.prose pre').forEach((pre) => {
    if (pre.parentElement?.classList.contains('code-block')) return;
    const wrap = document.createElement('div');
    wrap.className = 'code-block';
    pre.replaceWith(wrap);
    wrap.appendChild(pre);
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'code-copy';
    b.innerHTML = '<span data-copy-label>Copy</span><span class="visually-hidden"> code</span>';
    b.addEventListener('click', () => copy(pre.innerText.replace(/\n$/, ''), b));
    wrap.appendChild(b);
  });
}
