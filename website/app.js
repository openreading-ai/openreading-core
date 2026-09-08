/** Progressive enhancement leaves tutorial content and navigation usable without JavaScript. */
const toast = document.querySelector('.toast');
let toastTimer;
function announce(message) {
  toast.textContent = message;
  toast.classList.add('visible');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('visible'), 2500);
}
document.querySelectorAll('.copy').forEach(button => button.addEventListener('click', async () => {
  const code = button.closest('.code-block').querySelector('code');
  try {
    await navigator.clipboard.writeText(code.textContent);
    button.textContent = 'Copied';
    announce('Code copied');
    setTimeout(() => { button.textContent = 'Copy'; }, 2000);
  } catch {
    const range = document.createRange();
    range.selectNodeContents(code);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    announce('Code selected. Press ⌘C or Ctrl+C to copy.');
  }
}));
const sidebar = document.querySelector('.sidebar');
const menu = document.querySelector('.menu-toggle');
menu?.addEventListener('click', () => {
  menu.setAttribute('aria-expanded', String(sidebar.classList.toggle('open')));
});
document.querySelectorAll('[data-chapter]').forEach(link => link.addEventListener('click', () => {
  sidebar.classList.remove('open');
  menu?.setAttribute('aria-expanded', 'false');
}));
const search = document.querySelector('input[type="search"]');
search?.addEventListener('input', () => {
  const query = search.value.trim().toLowerCase();
  let count = 0;
  document.querySelectorAll('[data-chapter]').forEach(link => {
    const lesson = document.getElementById(link.dataset.chapter);
    link.hidden = !lesson.textContent.toLowerCase().includes(query);
    if (!link.hidden) count++;
  });
  document.querySelectorAll('.nav-group').forEach(group => {
    group.hidden = !group.querySelector('[data-chapter]:not([hidden])');
  });
  document.querySelector('.no-results').hidden = count > 0;
});
document.addEventListener('keydown', event => {
  if (event.key === '/' && search && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) && !document.activeElement.isContentEditable) {
    event.preventDefault();
    sidebar.classList.add('open');
    menu?.setAttribute('aria-expanded', 'true');
    search.focus();
  }
  if (event.key === 'Escape' && search) {
    search.value = '';
    search.dispatchEvent(new Event('input'));
    sidebar.classList.remove('open');
    menu?.setAttribute('aria-expanded', 'false');
  }
});
const lessons = [...document.querySelectorAll('[data-lesson]')];
let currentId;
let pending = false;
function updateChapter() {
  pending = false;
  if (!lessons.length) return;
  const current = lessons.filter(lesson => lesson.getBoundingClientRect().top <= 180).at(-1) ?? lessons[0];
  if (current.id === currentId) return;
  currentId = current.id;
  document.querySelectorAll('[data-chapter]').forEach(link => {
    if (link.dataset.chapter === currentId) link.setAttribute('aria-current', 'location');
    else link.removeAttribute('aria-current');
  });
  const sectionNav = document.getElementById('section-navigation');
  sectionNav.replaceChildren();
  current.querySelectorAll('h3').forEach(heading => {
    const link = document.createElement('a');
    link.href = `#${heading.id}`;
    link.textContent = heading.textContent;
    sectionNav.append(link);
  });
  if (!sectionNav.childElementCount) {
    const link = document.createElement('a');
    link.href = `#${current.id}`;
    link.textContent = current.querySelector('h2').textContent;
    sectionNav.append(link);
  }
  const number = Math.min(lessons.indexOf(current) + 1, 17);
  document.querySelector('progress').value = number;
  document.getElementById('progress-label').textContent = lessons.indexOf(current) === 17 ? 'Appendix' : `Step ${number} of 17`;
}
window.addEventListener('scroll', () => {
  if (!pending) { pending = true; requestAnimationFrame(updateChapter); }
}, { passive: true });
updateChapter();
