import { useState, type FormEvent } from 'react';
import { submitTask } from './api';

interface Props {
  /** called after the server accepted the post, so the page can refetch */
  onSubmitted?: () => void;
}

export function NewWorkForm({ onSubmitted }: Props) {
  const [text, setText] = useState('');
  const [cwd, setCwd] = useState('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ msg: string; error: boolean } | null>(null);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!text.trim() || busy) return;
    setBusy(true);
    setNotice(null);
    try {
      const msg = await submitTask({ text: text.trim(), cwd });
      const error = /^(A task needs|Task rejected|Task submission is not)/.test(msg);
      setNotice({ msg, error });
      if (!error) {
        setText('');
        onSubmitted?.();
      }
    } catch (err) {
      setNotice({ msg: err instanceof Error ? err.message : String(err), error: true });
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="newwork" onSubmit={onSubmit} aria-label="New work">
      <label className="field">
        <span>What should an agent do?</span>
        <textarea
          name="text"
          rows={3}
          required
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Describe the task"
        />
      </label>
      <label className="field">
        <span>Folder (optional)</span>
        <input
          name="cwd"
          type="text"
          value={cwd}
          onChange={(e) => setCwd(e.target.value)}
          placeholder="/home/you/Projects/thing"
          autoComplete="off"
          spellCheck={false}
        />
      </label>
      <div className="actions">
        <button type="submit" disabled={busy || !text.trim()}>
          {busy ? 'Starting…' : 'Start'}
        </button>
        {notice && (
          <p role="status" className={notice.error ? 'notice error' : 'notice'}>
            {notice.msg}
          </p>
        )}
      </div>
    </form>
  );
}
