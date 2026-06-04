type CallControlsProps = {
  sessionActive: boolean;
  connecting: boolean;
  startDisabled: boolean;
  liveMicActive: boolean;
  onStart: () => void;
  onStop: () => void;
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
        <span className={`live-pill ${props.liveMicActive ? "active" : ""}`}>
          {props.liveMicActive ? "Mic Live" : "Mic Idle"}
        </span>
      </div>
    </section>
  );
}
