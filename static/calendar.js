/* MAWOS :: the month calendar (#104), shared by the portal and the console.

   It draws what the server returned and decides nothing. Which entries a
   viewer may see is the CalendarAgent's decision, made on the server from the
   signed-in person's own record; the filter here only narrows that list.

   Dates stay ISO strings (YYYY-MM-DD) from the database to the screen. The
   grid is laid out with Date.UTC and compared as strings, so no browser time
   zone can move an entry onto the day before or after.

   Every field that came from a record goes through h() before it reaches
   innerHTML (#85); tests/auth_test.py 8e scans this file for one that does not. */
(function (root) {
  'use strict';
  const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September',
    'October', 'November', 'December'];
  const DOW = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const KINDS = [['cie', 'Internal exam (CIE)'], ['see', 'Semester exam (SEE)'], ['practical', 'Practical exam'],
    ['lab', 'Lab exam'], ['exam', 'Exam'], ['event', 'College event'], ['holiday', 'Holiday'], ['other', 'Other']];
  const EXAMS = ['cie', 'see', 'practical', 'lab', 'exam'];
  const SOURCE = {see: 'From the SEE timetable. It is changed there, not on the calendar.',
    cie: 'From the CIE scheme. It is changed there, not on the calendar.'};

  const h = s => String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const pad = n => String(n).padStart(2, '0');
  const iso = (y, m, d) => `${y}-${pad(m)}-${pad(d)}`;

  function ym(s) {
    const m = /^(\d{4})-(\d{2})$/.exec(String(s || ''));
    if (!m) throw new Error('month must be YYYY-MM: ' + s);
    return {y: +m[1], m: +m[2]};
  }
  // the month n months away; never through a Date, so 31 Jan + 1 is February
  function shift(s, n) {
    const {y, m} = ym(s), k = y * 12 + (m - 1) + n;
    return `${Math.floor(k / 12)}-${pad(k % 12 + 1)}`;
  }
  const daysIn = (y, m) => new Date(Date.UTC(y, m, 0)).getUTCDate();
  const lead = (y, m) => (new Date(Date.UTC(y, m - 1, 1)).getUTCDay() + 6) % 7;      // Monday first
  const weekday = d => (new Date(Date.UTC(+d.slice(0, 4), +d.slice(5, 7) - 1, +d.slice(8, 10))).getUTCDay() + 6) % 7;
  // the month as whole weeks: null for the days of the months either side
  function grid(s) {
    const {y, m} = ym(s), cells = [];
    for (let i = 0; i < lead(y, m); i++) cells.push(null);
    for (let d = 1; d <= daysIn(y, m); d++) cells.push(iso(y, m, d));
    while (cells.length % 7) cells.push(null);
    return cells;
  }
  const covers = (e, d) => e.date <= d && d <= (e.end_date || e.date);
  const title = s => { const {y, m} = ym(s); return `${MONTHS[m - 1]} ${y}`; };
  function longDate(d) {
    return `${DOW[weekday(d)]} ${+d.slice(8, 10)} ${MONTHS[+d.slice(5, 7) - 1].slice(0, 3)} ${d.slice(0, 4)}`;
  }
  const when = e => e.start_time ? e.start_time + (e.end_time ? '–' + e.end_time : '') : '';
  const label = e => e.kind_label || (KINDS.find(k => k[0] === e.kind) || [, e.kind])[1];

  function matches(e, filter) {
    if (!filter) return true;
    if (filter === 'exams') return EXAMS.includes(e.kind);
    return e.kind === filter;
  }

  // Entries that read the same on a day (the CIE scheme puts one IA test on
  // every class's calendar) are drawn once, with how many classes they cover.
  function groups(list) {
    const by = new Map();
    for (const e of list) {
      const k = [e.kind, e.title, e.start_time || ''].join('|');
      if (!by.has(k)) by.set(k, []);
      by.get(k).push(e);
    }
    return [...by.values()];
  }

  function pill(g, day) {
    const e = g[0], n = g.length;
    const what = n > 1 ? e.title + ' · ' + n + ' classes' : e.title;      // plain text: escaped where it is used
    const aria = `${label(e)}: ${what}${when(e) ? ', ' + when(e) : ''}${n === 1 ? ', for ' + e.audience : ''}`;
    const tgt = n > 1 ? `data-cal-day="${h(day)}" data-cal-group="${h(e.kind + '|' + e.title)}"` : `data-cal-ev="${h(e.id)}"`;
    return `<button type="button" class="cal-pill k-${h(e.kind)}" ${tgt} title="${h(aria)}" aria-label="${h(aria)}">` +
      `${e.start_time ? `<span class="cal-t">${h(e.start_time)}</span>` : ''}<span class="cal-x">${h(what)}</span></button>`;
  }

  /* el: where to draw. o: {month: 'YYYY-MM', events: [...], today: 'YYYY-MM-DD', filter, selected} */
  function render(el, o) {
    const list = (o.events || []).filter(e => matches(e, o.filter));
    const cells = grid(o.month);
    const MAX = 3;
    let html = `<div class="cal-grid" role="grid" aria-label="${h(title(o.month))}">` +
      `<div class="cal-row cal-head" role="row">${DOW.map(d => `<div class="cal-dow" role="columnheader">${d}</div>`).join('')}</div>`;
    for (let r = 0; r < cells.length; r += 7) {
      html += '<div class="cal-row" role="row">';
      for (const d of cells.slice(r, r + 7)) {
        if (!d) { html += '<div class="cal-day cal-out" role="gridcell" aria-hidden="true"></div>'; continue; }
        const mine = list.filter(e => covers(e, d));
        const gs = groups(mine);
        const hol = mine.some(e => e.kind === 'holiday');
        const cls = ['cal-day', d === o.today ? 'cal-today' : '', weekday(d) === 6 ? 'cal-sun' : '',
          hol ? 'cal-hol' : '', d === o.selected ? 'cal-sel' : ''].filter(Boolean).join(' ');
        html += `<div class="${cls}" role="gridcell" aria-label="${h(longDate(d))}${mine.length ? ', ' + mine.length + ' entr' + (mine.length === 1 ? 'y' : 'ies') : ''}">` +
          `<button type="button" class="cal-n" data-cal-day="${h(d)}" aria-label="${h(longDate(d))}">${+d.slice(8, 10)}</button>` +
          gs.slice(0, MAX).map(g => pill(g, d)).join('') +
          (gs.length > MAX ? `<button type="button" class="cal-more" data-cal-day="${h(d)}">+${gs.length - MAX} more</button>` : '') +
          `<span class="cal-dots" aria-hidden="true">${gs.slice(0, 4).map(g => `<i class="k-${h(g[0].kind)}"></i>`).join('')}</span>` +
          '</div>';
      }
      html += '</div>';
    }
    html += '</div>';
    el.innerHTML = html;
  }

  function legend() {
    return `<div class="cal-legend" aria-label="Kinds of entry">${KINDS.map(([k, l]) =>
      `<span><i class="k-${k}"></i>${h(l)}</span>`).join('')}</div>`;
  }

  // the list of a month (or one day), soonest first: the calendar's text form,
  // and what a phone shows under the grid
  function agenda(list, filter, empty) {
    const xs = (list || []).filter(e => matches(e, filter));
    if (!xs.length) return `<div class="cal-empty">${h(empty || 'Nothing on the calendar.')}</div>`;
    let out = '<ol class="cal-agenda">', last = '';
    for (const e of xs) {
      if (e.date !== last) { out += `<li class="cal-ad">${h(longDate(e.date))}</li>`; last = e.date; }
      out += `<li><button type="button" class="cal-ai k-${h(e.kind)}" data-cal-ev="${h(e.id)}">` +
        `<span class="cal-t">${h(when(e) || 'All day')}</span><span class="cal-x"><b>${h(e.title)}</b>` +
        `<small>${h(label(e))} · ${h(e.audience)}${e.venue ? ' · ' + h(e.venue) : ''}</small></span></button></li>`;
    }
    return out + '</ol>';
  }

  // one entry: only the fields that apply to it (#104, section 9)
  function detail(e, opt) {
    opt = opt || {};
    const rows = [['Date', longDate(e.date) + (e.end_date ? ' to ' + longDate(e.end_date) : '')]];
    if (e.start_time) rows.push(['Time', when(e)]);
    if (e.subject) rows.push(['Subject', `${e.subject}${e.subject_name ? ' ' + e.subject_name : ''}`]);
    rows.push(['For', e.audience]);
    if (e.venue) rows.push(['Where', e.venue]);
    if (e.description) rows.push(['Details', e.description]);
    if (opt.admin && e.source === 'calendar') rows.push(['Entry', '#' + e.id]);
    let html = `<div class="cal-det"><div class="cal-kind k-${h(e.kind)}">${h(label(e))}</div>` +
      `<h3>${h(e.title)}</h3><dl>${rows.map(([k, v]) => `<dt>${h(k)}</dt><dd>${h(v)}</dd>`).join('')}</dl>`;
    if (SOURCE[e.source]) html += `<p class="cal-src">${h(SOURCE[e.source])}</p>`;
    if (opt.admin && e.editable) {
      html += `<div class="cal-acts"><button type="button" class="btn sm" data-cal-edit="${h(e.id)}">Change it</button>` +
        `<button type="button" class="btn sm ghost" data-cal-cancel="${h(e.id)}">Cancel this entry</button></div>`;
    }
    return html + '</div>';
  }

  const api = {MONTHS, DOW, KINDS, EXAMS, grid, shift, daysIn, lead, weekday, covers, title, longDate, when,
    matches, groups, render, legend, agenda, detail, h};
  root.MawosCalendar = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
