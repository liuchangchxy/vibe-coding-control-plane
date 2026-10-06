#!/usr/bin/env node
import { runOnboardingCli } from "../lib/onboarding/cli.mjs";

process.exitCode = await runOnboardingCli(["audit", ...process.argv.slice(2)]);
