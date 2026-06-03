import type { SessionMode } from "../realtime/events";

type CallControlsProps = {
  sessionActive: boolean;
  connecting: boolean;
  startDisabled: boolean;
  pushToTalkActive: boolean;
  autoCommit: boolean;
  mode: SessionMode;
  onStart: () => void;
  onStop: () => void;
  onPushToTalkStart: () => void;
  onPushToTalkEnd: () => void;
  onCommit: () => void;
  onCancel: () => void;
  onToggleAutoCommit: (value: boolean) => void;
  onModeChange: (mode: SessionMode) => void;
};

export function CallControls(props: CallControlsProps) {
  const disabled = props.connecting;

  return (
    <section className="panel controls-panel">
      <div className="controls-row">
        <button
          className="button primary"
          onClick={props.onStart}
          disabled={props.startDisabled || props.sessionActive || disabled}
        >
          Start Session
        </button>
        <button className="button" onClick={props.onStop} disabled={!props.sessionActive && !props.connecting}>
          Stop Session
        </button>
        <button
          className={`button ptt ${props.pushToTalkActive ? "active" : ""}`}
          onMouseDown={props.onPushToTalkStart}
          onMouseUp={props.onPushToTalkEnd}
          onMouseLeave={props.onPushToTalkEnd}
          onTouchStart={(event) => {
            event.preventDefault();
            props.onPushToTalkStart();
          }}
          onTouchEnd={(event) => {
            event.preventDefault();
            props.onPushToTalkEnd();
          }}
          disabled={!props.sessionActive}
        >
          Push To Talk
        </button>
        <button className="button" onClick={props.onCommit} disabled={!props.sessionActive}>
          Commit
        </button>
        <button className="button danger" onClick={props.onCancel} disabled={!props.sessionActive}>
          Cancel Response
        </button>
      </div>

      <div className="controls-row">
        <div className="segmented-control" role="tablist" aria-label="Response mode">
          <button
            className={`segment ${props.mode === "text" ? "selected" : ""}`}
            onClick={() => props.onModeChange("text")}
            disabled={props.sessionActive}
          >
            Text Only
          </button>
          <button
            className={`segment ${props.mode === "text,audio" ? "selected" : ""}`}
            onClick={() => props.onModeChange("text,audio")}
            disabled={props.sessionActive}
          >
            Text + Audio
          </button>
        </div>

        <label className="checkbox">
          <input
            type="checkbox"
            checked={props.autoCommit}
            onChange={(event) => props.onToggleAutoCommit(event.target.checked)}
          />
          <span>Auto Commit</span>
        </label>
      </div>
    </section>
  );
}
