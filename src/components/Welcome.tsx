interface Props {
  dragging: boolean;
  busy: boolean;
  onChoose: () => void;
}

export default function Welcome({ dragging, busy, onChoose }: Props) {
  return (
    <div className={`welcome ${dragging ? "dragging" : ""}`}>
      <div className="welcome-inner">
        <span className="eyebrow">Local-first culling for macOS</span>
        <h1>Find the keepers.<br />Leave the originals alone.</h1>
        <p className="welcome-copy">
          Import a shoot, generate review proxies, and get a first-pass cull with sharpness,
          duplicate, and burst suggestions. Nothing is deleted, moved, or renamed.
        </p>
        <div className="drop-zone">
          <div className="drop-icon" aria-hidden="true">
            <span />
          </div>
          <div>
            <strong>Drop a shoot folder here</strong>
            <span className="muted">JPEG, HEIC, and common RAW · scanned recursively</span>
          </div>
          <button type="button" className="primary" onClick={onChoose} disabled={busy}>
            {busy ? "Opening…" : "Choose Folder"}
          </button>
        </div>
        <div className="welcome-points">
          <div>
            <strong>Read-only sources</strong>
            <span>Topshot never writes into the folder you import.</span>
          </div>
          <div>
            <strong>Explicit copy</strong>
            <span>Only confirmed keeps are copied, after a summary confirmation.</span>
          </div>
          <div>
            <strong>Verified files</strong>
            <span>Every copy is SHA-256 checked and atomically placed.</span>
          </div>
          <div>
            <strong>Private by default</strong>
            <span>No photos, filenames, or metadata leave this machine.</span>
          </div>
        </div>
        <p className="fineprint">
          Videos are skipped and counted. RAW+JPEG pairs copy together by default. Model-based
          analysis is not enabled in this increment.
        </p>
      </div>
    </div>
  );
}
