import { useCallback, useEffect, useRef, useState } from "react";
import { Mic, Volume2, VolumeX } from "lucide-react";
import { ApiError, api, onEvent, type FleetEvent, type InboxPlan } from "./api";
import { go } from "./hooks";

/** Push-to-talk (gate V3): the browser's own speech recognition turns words into text, Fleet's grammar turns the text
 * into a command or a task (POST /api/voice/...), and speechSynthesis reads the answer back - plans waiting in the
 * Inbox, a click that needs approval, a finished task. No model, no cost, nothing leaves the browser but the words
 * sent to Fleet's own API. Chrome and Edge only (the Web Speech API); elsewhere the button says so. */

// The Web Speech API isn't in TypeScript's DOM lib: the part of it this component uses.
interface Recognition {
  lang: string;
  interimResults: boolean;
  maxAlternatives: number;
  continuous: boolean;
  onresult: ((e: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void) | null;
  onend: (() => void) | null;
  onerror: ((e: { error: string }) => void) | null;
  start(): void;
  stop(): void;
  abort(): void;
}
type RecognitionCtor = new () => Recognition;

function recognitionClass(): RecognitionCtor | null {
  const w = window as unknown as { SpeechRecognition?: RecognitionCtor; webkitSpeechRecognition?: RecognitionCtor };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

const LANGS: [string, string][] = [["pl-PL", "PL"], ["en-US", "EN"]];
const APPROVE = /^(approve|approved|yes|ok|okay|zatwierdź|zatwierdzam|potwierdź|potwierdzam|zgoda|zgadzam się|tak)[.!]?$/i;
const REJECT = /^(reject|rejected|no|nope|odrzuć|odrzucam|nie)[.!]?$/i;
// "open wikipedia.org" with no page open yet: the session starts there instead of answering "no session".
const OPEN = /^(?:open|go to|visit|otwórz|wejdź na|przejdź (?:na|do))\s+((?:https?:\/\/)?[\w-]+(?:\.[\w-]+)+\S*)[.!]?$/i;

function remembered(key: string, fallback: string): string {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

export function Voice({ plans }: { plans: InboxPlan[] }) {
  const supported = recognitionClass() !== null;
  const [lang, setLang] = useState(() => remembered("fleet.voice.lang", navigator.language.startsWith("pl") ? "pl-PL" : "en-US"));
  const [readback, setReadback] = useState(() => remembered("fleet.voice.readback", "on") === "on");
  const [listening, setListening] = useState(false);
  const [busy, setBusy] = useState(false);
  const [heard, setHeard] = useState("");
  const [said, setSaid] = useState(supported ? "" : "Voice needs Chrome or Edge.");
  const recognizer = useRef<Recognition | null>(null);
  const session = useRef<string | null>(null);
  const pendingClick = useRef(false); // the last command was a click that waits for "approve" on the page
  const plansRef = useRef(plans);
  plansRef.current = plans;

  const speak = useCallback(
    (text: string) => {
      setSaid(text);
      if (!readback || !("speechSynthesis" in window) || !text) return;
      window.speechSynthesis.cancel();
      const utterance = new SpeechSynthesisUtterance(text.length > 400 ? text.slice(0, 400) + "…" : text);
      utterance.lang = lang;
      window.speechSynthesis.speak(utterance);
    },
    [lang, readback],
  );

  // Read aloud what the operator would otherwise have to look at: a plan waiting, a request for help, a task finished.
  useEffect(
    () =>
      onEvent((ev: FleetEvent) => {
        if (ev.type === "plan") speak("A plan waits for your approval in the Inbox. Say approve or reject.");
        else if (ev.type === "human.request") speak(`Fleet needs you: ${ev.message}`);
        else if (ev.type === "task.status" && (ev.data?.status === "done" || ev.data?.status === "failed")) speak(`Task ${ev.data.status}. ${ev.message}`);
      }),
    [speak],
  );

  const openSession = useCallback(async (url: string) => {
    const r = await api.voiceSession(url);
    session.current = r.session_id;
    pendingClick.current = false;
    return r;
  }, []);

  const handle = useCallback(
    async (text: string) => {
      const words = text.trim();
      if (!words) return;
      setHeard(words);
      setBusy(true);
      try {
        // "approve" / "reject" answer a plan in the Inbox unless a click on the page is what waits.
        if (!pendingClick.current && plansRef.current.length && (APPROVE.test(words) || REJECT.test(words))) {
          const plan = plansRef.current[0];
          if (APPROVE.test(words)) {
            await api.approveTask(plan.id);
            speak(`Plan approved: ${plan.prompt.slice(0, 80)}`);
          } else {
            await api.cancelTask(plan.id);
            speak(`Plan rejected: ${plan.prompt.slice(0, 80)}`);
          }
          return;
        }
        let result;
        try {
          result = await api.voiceCommand(session.current, words);
        } catch (e) {
          if (!(e instanceof ApiError) || e.status !== 404) throw e;
          const open = OPEN.exec(words);
          if (!open) {
            speak("No page is open yet. Say: open, and a site.");
            return;
          }
          const url = open[1].startsWith("http") ? open[1] : `https://${open[1]}`;
          await openSession(url);
          speak(`Opened ${url}`);
          return;
        }
        pendingClick.current = !!result.result.needs_approval;
        if (result.result.task_id) go(`/tasks/${result.result.task_id}`);
        speak(result.result.said || (result.result.ok ? "Done." : "That didn't work."));
      } catch (e) {
        speak(e instanceof ApiError ? e.message : "Fleet isn't answering.");
      } finally {
        setBusy(false);
      }
    },
    [openSession, speak],
  );

  const listen = useCallback(() => {
    const Ctor = recognitionClass();
    if (!Ctor) return;
    if (recognizer.current) {
      recognizer.current.stop();
      return;
    }
    const r = new Ctor();
    r.lang = lang;
    r.interimResults = false;
    r.maxAlternatives = 1;
    r.continuous = false;
    r.onresult = (e) => {
      const transcript = e.results[0]?.[0]?.transcript ?? "";
      if (transcript) void handle(transcript);
    };
    r.onerror = (e) => {
      if (e.error !== "aborted" && e.error !== "no-speech") speak(e.error === "not-allowed" ? "The microphone is blocked for this page." : `Microphone: ${e.error}`);
    };
    r.onend = () => {
      recognizer.current = null;
      setListening(false);
    };
    recognizer.current = r;
    setListening(true);
    window.speechSynthesis?.cancel();
    r.start();
  }, [handle, lang, speak]);

  useEffect(() => () => recognizer.current?.abort(), []);

  const toggleLang = () => {
    const next = LANGS[(LANGS.findIndex(([code]) => code === lang) + 1) % LANGS.length][0];
    setLang(next);
    try {
      localStorage.setItem("fleet.voice.lang", next);
    } catch {
      /* private window */
    }
  };
  const toggleReadback = () => {
    const next = !readback;
    setReadback(next);
    if (!next) window.speechSynthesis?.cancel();
    try {
      localStorage.setItem("fleet.voice.readback", next ? "on" : "off");
    } catch {
      /* private window */
    }
  };

  return (
    <div className={`voice ${listening ? "listening" : ""}`}>
      <div className="row gap center">
        <button
          className={`btn mic ${listening ? "primary" : ""}`}
          onClick={listen}
          disabled={!supported || busy}
          title={supported ? (listening ? "Listening… click to stop" : "Push to talk") : "Voice needs Chrome or Edge (Web Speech API)"}
          aria-pressed={listening}
        >
          <Mic size={16} />
          <span>{listening ? "Listening…" : busy ? "Working…" : "Talk"}</span>
        </button>
        <button className="btn small" onClick={toggleLang} title="Language of the spoken words" disabled={!supported}>
          {LANGS.find(([code]) => code === lang)?.[1]}
        </button>
        <button className="btn small" onClick={toggleReadback} title={readback ? "Readback on: click to mute" : "Readback off"}>
          {readback ? <Volume2 size={14} /> : <VolumeX size={14} />}
        </button>
      </div>
      {heard && <div className="muted tiny voice-line" title="What was heard">“{heard}”</div>}
      {said && <div className="tiny voice-line" title="What Fleet said">{said}</div>}
    </div>
  );
}
