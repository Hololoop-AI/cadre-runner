// New work: the dialog the header's + New work opens (statusd._new_work_form,
// _run_picker and the page script's hint), and New project folded under the
// list (statusd._new_project_form).

import { useEffect, useMemo, useRef, useState } from 'react';
import { findEntries, folderPicks, liveProjects, projectHint, type LibraryEntry, type Project } from '../model';
import { DirField } from './DirField';

const NOBODY = 'the session picks its own skill — click to choose one yourself';

function RunPicker({ lib }: { lib: LibraryEntry[] }) {
  const [q, setQ] = useState('');
  const [val, setVal] = useState('');
  const [summary, setSummary] = useState(NOBODY);
  const [items, setItems] = useState<LibraryEntry[] | null>(null);
  const [shown, setShown] = useState(false);
  const [cur, setCur] = useState(-1);
  const [marked, setMarked] = useState(false);
  const hide = () => {
    setShown(false);
    setCur(-1);
  };
  const find = (v: string) => {
    if (!v.trim()) return hide();
    setCur(-1);
    setMarked(false);
    setItems(findEntries(lib, v));
    setShown(true);
  };
  const pick = (n: number) => {
    if (!items || n < 0 || n >= items.length) return;
    const e = items[n];
    setVal(e.v);
    setQ(e.i);
    hide();
    setSummary('run ' + e.i);
  };
  return (
    <details className="pick">
      <summary>{summary}</summary>
      <input type="hidden" name="run" id="runval" value={val} />
      <div className="dirwrap">
        <input id="runq" type="text" autoComplete="off" spellCheck="false" role="combobox"
          aria-expanded={shown} aria-controls="runsug" aria-autocomplete="list"
          placeholder="type to find — build, review, spanish…" value={q}
          onChange={(e) => { setQ(e.target.value); find(e.target.value); }}
          onFocus={(e) => find(e.target.value)}
          onBlur={() => setTimeout(hide, 120)}
          onKeyDown={(e) => {
            if (e.key === 'Escape') return hide();
            if (!shown || !items || !items.length) return;
            if (e.key === 'ArrowDown') { e.preventDefault(); setMarked(true); setCur((cur + 1) % items.length); }
            else if (e.key === 'ArrowUp') { e.preventDefault(); setMarked(true); setCur((cur <= 0 ? items.length : cur) - 1); }
            else if (e.key === 'Enter') { e.preventDefault(); pick(cur < 0 ? 0 : cur); }
            else if (e.key === 'Tab' && items.length === 1) { e.preventDefault(); pick(0); }
          }} />
        <ul id="runsug" className="sug" role="listbox" hidden={!shown}>
          {items && !items.length && <li className="none">nothing installed matches that</li>}
          {items?.map((e, n) => (
            <li key={e.v} role="option" className={marked ? (n === cur ? 'on' : '') : undefined}
              onMouseDown={(ev) => { ev.preventDefault(); pick(n); }}>
              <b>{e.i}</b><span className="hint">{e.d}</span>
            </li>
          ))}
        </ul>
      </div>
      <p className="meta">Leave this alone and the session chooses. <button type="button" id="runclear" className="linky"
        onClick={() => { setVal(''); setQ(''); hide(); setSummary(NOBODY); }}>clear</button></p>
    </details>
  );
}

export interface NewWorkProps {
  open: boolean;
  /** the page was opened with ?parent=: the server marks the dialog to open itself */
  dataOpen?: boolean;
  /** the item to preselect; null keeps what is selected */
  parent: string | null;
  parents: [string, string][];
  projs: Record<string, Project>;
  home: string;
  lib: LibraryEntry[];
  /** why the last Start did not reach the server, shown in the dialog */
  error?: string;
  onClose: () => void;
  onSubmit: (form: HTMLFormElement) => void;
}

/** The New work dialog the header's + New work (or an item's New work under this) opens. */
export function NewWorkDialog({ open, dataOpen, parent, parents, projs, home, lib, error, onClose, onSubmit }: NewWorkProps) {
  const dlg = useRef<HTMLDialogElement>(null);
  const what = useRef<HTMLTextAreaElement>(null);
  const [dir, setDir] = useState('');
  const [under, setUnder] = useState(parent ?? '');
  useEffect(() => {
    const d = dlg.current;
    if (!d) return;
    if (open) {
      if (parent != null) setUnder(parent);
      if (d.showModal && !d.open) d.showModal();
      else d.setAttribute('open', ''); // jsdom has no showModal
      what.current?.focus();
    } else if (d.open) d.close();
  }, [open, parent]);
  const picks = useMemo(() => folderPicks(projs, home), [projs, home]);
  return (
    <dialog id="newwork" ref={dlg} onClose={onClose} data-open={dataOpen ? '' : undefined}>
      <form className="newtask" method="post" action="/tasks"
        onSubmit={(e) => { e.preventDefault(); onSubmit(e.currentTarget); }}>
        <h3>Start work</h3>
        <label htmlFor="what">What should the agent do?</label>
        <textarea ref={what} id="what" name="text" rows={3} required
          placeholder="e.g. Find out why Study cards are not useful yet"></textarea>
        <DirField fid="workdir" lid="worksug" label="Folder it works in" name="cwd" value={dir} onChange={setDir} />
        <div className="sugg">
          {picks.map((d) => <button key={d} type="button" data-dir={d} onClick={() => setDir(d)}>{d}</button>)}
        </div>
        <p className="hint" id="projhint">{projectHint(dir, home, projs)}</p>
        <label htmlFor="under">Put it under an item (optional)</label>
        <select id="under" name="parent" value={under} onChange={(e) => setUnder(e.target.value)}>
          <option value="">New item of its own</option>
          {parents.map(([k, t]) => <option key={k} value={k}>{[...t].slice(0, 90).join('')}</option>)}
        </select>
        <label htmlFor="newtags">Tags</label>
        <input id="newtags" name="tags" placeholder="comma separated, e.g. cadre, ux" />
        <label>Use a skill (optional)</label>
        <RunPicker lib={lib} />
        {error && <p className="notice">{error}</p>}
        <div className="dlgacts">
          <button type="button" className="ghost" id="cancelwork" onClick={() => dlg.current?.close()}>Cancel</button>
          <button type="submit" className="primary">Start</button>
        </div>
      </form>
    </dialog>
  );
}

/** New project, folded under the list; a plain form post that lands on the new project's page. */
export function NewProjectForm({ projs }: { projs: Record<string, Project> }) {
  const [dir, setDir] = useState('');
  return (
    <details className="card newproject">
      <summary className="meta">New project — a durable home for work, kept on the fleet until you archive it</summary>
      <form className="newtask" method="post" action="/projects">
        <div className="row"><input name="name" required maxLength={64} placeholder="name, e.g. Cadre" /></div>
        <DirField fid="dir1" lid="dirsug" label="Working directory" value={dir} onChange={setDir} />
        <div className="row">
          <select name="group" defaultValue="">
            <option value="">no group</option>
            {liveProjects(projs).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
          <input name="context" type="text" placeholder="context store path (optional): a folder with brief.md and decisions/" />
          <button type="submit">Create</button>
        </div>
      </form>
    </details>
  );
}
