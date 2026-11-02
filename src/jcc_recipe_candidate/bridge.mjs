// Internal bridge. Python validates and normalizes all input before this call.
// craftableFrom is an unchanged MIT-licensed upstream excerpt; see vendor/SOURCE.md.
import { readFileSync } from 'node:fs';
import { craftableFrom } from './vendor/craftableFrom.mjs';

const { heldComponents, recipes } = JSON.parse(readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(craftableFrom(heldComponents, recipes)));
