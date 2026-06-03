type TranscriptPanelProps = {
  title: string;
  text: string;
  placeholder: string;
};

export function TranscriptPanel(props: TranscriptPanelProps) {
  return (
    <section className="panel transcript-panel">
      <header className="panel-header">
        <h2>{props.title}</h2>
      </header>
      <div className="panel-body transcript-body">
        {props.text ? props.text : <span className="muted">{props.placeholder}</span>}
      </div>
    </section>
  );
}

