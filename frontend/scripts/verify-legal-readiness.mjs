// Dependency-free release gate; Node 22.18+ native type stripping.
import { legalReadinessIssues } from '../src/privacy/legalConfig.ts';
const issues = legalReadinessIssues();
if (issues.length) {
  console.error('LEGAL GATE — production privacy readiness FAILED:\n' + issues.map(issue => `- ${issue}`).join('\n'));
  process.exitCode = 1;
} else console.log('Production legal configuration checks passed; external legal/Ops sign-off remains required.');
