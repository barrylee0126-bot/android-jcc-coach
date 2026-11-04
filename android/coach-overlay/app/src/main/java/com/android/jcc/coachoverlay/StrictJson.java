package com.android.jcc.coachoverlay;

import java.util.LinkedHashMap;
import java.util.Map;

/** Small strict JSON object parser for the fixed local overlay envelope. */
final class StrictJson {
    private StrictJson() { }

    static Map<String, Object> object(String raw) throws ParseException {
        if (raw == null || raw.length() > 4096) throw new ParseException("size");
        Parser parser = new Parser(raw);
        Map<String, Object> value = parser.object(0);
        parser.space();
        if (!parser.atEnd()) throw new ParseException("trailing");
        return value;
    }

    static final class ParseException extends Exception {
        ParseException(String reason) { super(reason); }
    }

    private static final class Parser {
        private final String input;
        private int index;

        Parser(String input) { this.input = input; }

        Map<String, Object> object(int depth) throws ParseException {
            if (depth > 2) throw new ParseException("depth");
            space();
            expect('{');
            space();
            Map<String, Object> output = new LinkedHashMap<>();
            if (take('}')) return output;
            while (true) {
                space();
                String key = string();
                if (output.containsKey(key)) throw new ParseException("duplicate_key");
                space();
                expect(':');
                Object value = value(depth + 1);
                output.put(key, value);
                space();
                if (take('}')) return output;
                expect(',');
            }
        }

        Object value(int depth) throws ParseException {
            space();
            if (atEnd()) throw new ParseException("value");
            char current = input.charAt(index);
            if (current == '"') return string();
            if (current == '{') return object(depth);
            if (takeLiteral("true")) return Boolean.TRUE;
            if (takeLiteral("false")) return Boolean.FALSE;
            if (takeLiteral("null")) return null;
            if (current == '-' || (current >= '0' && current <= '9')) return integer();
            throw new ParseException("value");
        }

        String string() throws ParseException {
            expect('"');
            StringBuilder value = new StringBuilder();
            while (!atEnd()) {
                char current = input.charAt(index++);
                if (current == '"') return value.toString();
                if (current < 0x20) throw new ParseException("control");
                if (current != '\\') { value.append(current); continue; }
                if (atEnd()) throw new ParseException("escape");
                char escaped = input.charAt(index++);
                switch (escaped) {
                    case '"': value.append('"'); break;
                    case '\\': value.append('\\'); break;
                    case '/': value.append('/'); break;
                    case 'b': value.append('\b'); break;
                    case 'f': value.append('\f'); break;
                    case 'n': value.append('\n'); break;
                    case 'r': value.append('\r'); break;
                    case 't': value.append('\t'); break;
                    case 'u': value.append((char) hex4()); break;
                    default: throw new ParseException("escape");
                }
            }
            throw new ParseException("string");
        }

        Long integer() throws ParseException {
            int start = index;
            take('-');
            if (atEnd()) throw new ParseException("integer");
            if (take('0')) {
                if (!atEnd() && asciiDigit(input.charAt(index))) throw new ParseException("integer");
            } else {
                if (atEnd() || input.charAt(index) < '1' || input.charAt(index) > '9') throw new ParseException("integer");
                while (!atEnd() && asciiDigit(input.charAt(index))) index++;
            }
            try { return Long.parseLong(input.substring(start, index)); }
            catch (NumberFormatException error) { throw new ParseException("integer_range"); }
        }

        int hex4() throws ParseException {
            if (index + 4 > input.length()) throw new ParseException("unicode");
            int value = 0;
            for (int position = 0; position < 4; position++) {
                int digit = asciiHex(input.charAt(index++));
                if (digit < 0) throw new ParseException("unicode");
                value = value * 16 + digit;
            }
            return value;
        }

        private boolean asciiDigit(char value) {
            return value >= '0' && value <= '9';
        }

        private int asciiHex(char value) {
            if (value >= '0' && value <= '9') return value - '0';
            if (value >= 'a' && value <= 'f') return value - 'a' + 10;
            if (value >= 'A' && value <= 'F') return value - 'A' + 10;
            return -1;
        }

        void space() {
            while (!atEnd() && (input.charAt(index) == ' ' || input.charAt(index) == '\n'
                    || input.charAt(index) == '\r' || input.charAt(index) == '\t')) index++;
        }

        boolean take(char expected) {
            if (!atEnd() && input.charAt(index) == expected) { index++; return true; }
            return false;
        }

        boolean takeLiteral(String expected) {
            if (!input.regionMatches(index, expected, 0, expected.length())) return false;
            index += expected.length();
            return true;
        }

        void expect(char expected) throws ParseException {
            if (!take(expected)) throw new ParseException("expected_" + expected);
        }

        boolean atEnd() { return index >= input.length(); }
    }
}
