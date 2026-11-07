// statusd._dir_field: a directory input that completes from this machine
// through /fs, with its own suggestion list (arrows, Enter, Tab, Escape).

import { useRef, useState } from 'react';
import { useServer } from './server';

/** A folder field with completion; the caller holds its value. */
export function DirField({ fid, lid, label, name, value, onChange }: {
  fid: string; lid: string; label: string; name?: string;
  value: string; onChange: (v: string) => void;
}) {
  // hiding keeps the last list drawn, as the Python page's hide() does
  const [items, setItems] = useState<string[] | null>(null);
  const [shown, setShown] = useState(false);
  const [cur, setCur] = useState(-1);
  // the arrow keys mark the list; until then its rows carry no class
  const [marked, setMarked] = useState(false);
  const t = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const asked = useRef(0);
  const input = useRef<HTMLInputElement>(null);
  const server = useServer();

  const hide = () => {
    setShown(false);
    setCur(-1);
  };
  // Nothing is drawn until the driver types: an unprompted list of every
  // directory in $HOME is noise. Only the newest question's answer is drawn.
  const load = (q: string) => {
    if (!q) return hide();
    const n = ++asked.current;
    server.fetchDirs(q).then((d) => {
      if (n !== asked.current) return;
      setItems(d);
      setShown(true);
      setCur(-1);
      setMarked(false);
    }).catch(() => {});
  };
  const pick = (n: number) => {
    if (!items || n < 0 || n >= items.length) return;
    onChange(items[n]);
    input.current?.focus();
    load(items[n]);
  };
  return (
    <div className="dirwrap">
      <label className="lbl" htmlFor={fid}>{label} — type <code>/</code> or <code>~</code> and it completes from this machine</label>
      <input
        ref={input} name={name || fid} id={fid} autoComplete="off" spellCheck="false" role="combobox"
        aria-expanded={shown} aria-autocomplete="list" aria-controls={lid} placeholder="~/Projects/…"
        value={value}
        onChange={(e) => {
          onChange(e.target.value);
          clearTimeout(t.current);
          const q = e.target.value;
          t.current = setTimeout(() => load(q), 80);
        }}
        onBlur={() => setTimeout(hide, 120)}
        onKeyDown={(e) => {
          if (e.key === 'Escape') return hide();
          if (!shown || !items || !items.length) return;
          if (e.key === 'ArrowDown') {
            e.preventDefault();
            setMarked(true);
            setCur((cur + 1) % items.length);
          } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            setMarked(true);
            setCur((cur <= 0 ? items.length : cur) - 1);
          } else if (e.key === 'Enter' && cur >= 0) {
            e.preventDefault();
            pick(cur);
          } else if (e.key === 'Tab' && items.length === 1) {
            e.preventDefault();
            pick(0);
          }
        }}
      />
      <ul id={lid} className="sug" role="listbox" hidden={!shown}>
        {items && !items.length && <li className="none">no directory matches</li>}
        {items?.map((p, n) => (
          <li key={p} role="option" className={marked ? (n === cur ? 'on' : '') : undefined}
            onMouseDown={(e) => { e.preventDefault(); pick(n); }}>{p}</li>
        ))}
      </ul>
    </div>
  );
}
