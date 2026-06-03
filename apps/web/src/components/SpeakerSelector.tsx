import type { Speaker } from "../realtime/events";

type SpeakerSelectorProps = {
  speaker: Speaker;
  disabled: boolean;
  onChange: (speaker: Speaker) => void;
};

const speakers: Speaker[] = ["Ethan", "Chelsie", "Aiden"];

export function SpeakerSelector(props: SpeakerSelectorProps) {
  return (
    <label className="field">
      <span className="field-label">Speaker</span>
      <select
        className="select"
        value={props.speaker}
        onChange={(event) => props.onChange(event.target.value as Speaker)}
        disabled={props.disabled}
      >
        {speakers.map((speaker) => (
          <option key={speaker} value={speaker}>
            {speaker}
          </option>
        ))}
      </select>
    </label>
  );
}

