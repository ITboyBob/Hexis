import { notFound } from "next/navigation";

import ControlledToolActivityScenario from "./scenario";

export const dynamic = "force-dynamic";

export default function ControlledToolActivityPage() {
  if (process.env.BOB108_CONTROLLED_RUNTIME !== "1") notFound();
  return <ControlledToolActivityScenario />;
}
