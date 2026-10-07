import { notFound } from "next/navigation";
import WorkerHarness from "./worker-harness";

export const dynamic = "force-dynamic";
export const revalidate = 0;

export default function VoiceVadTestPage() {
  if (process.env.HEXIS_VAD_TEST_HARNESS !== "1") notFound();
  return <WorkerHarness />;
}
