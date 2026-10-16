# ShieldLang grammar

## Lexical structure

| Token | Definition |
|---|---|
| Comment | `// …`, `# …`, `/* … */` |
| IDENT | `[A-Za-z_][A-Za-z0-9_]*` (if it is not a keyword) |
| NUMBER | `[0-9][0-9_]*` with an optional fraction `.[0-9]+` |
| STRING | `"…"` with escapes `\n \t \" \\` |
| REGEX | `/body/flags`. Recognised only where an operand is expected. The only flag is `i`. |
| Units | `B`, `KB`, `MB` after a number (`16 KB` = 16384) |
| Keywords | `rule on const pattern header query field param body when require forbid block if any matches minlen maxlen in maxitems minitems max_depth max_size max_keys as string int number bool list enum true false exists and or not path` |

## Syntax (EBNF)

```ebnf
program     = { item } ;
item        = const | pattern | rule ;
const       = "const" IDENT "=" expr ";" ;
pattern     = "pattern" IDENT "=" REGEX ";" ;
rule        = "rule" IDENT "on" METHOD STRING "{" { stmt } "}" ;
METHOD      = "GET" | "POST" | "PUT" | "PATCH" | "DELETE" | "HEAD" | "OPTIONS" | "ANY" ;

stmt        = fieldStmt | whenStmt | blockStmt | bodyStmt ;
fieldStmt   = source name [ "?" ] ":" type { constraint } ";" ;
source      = "header" | "query" | "field" | "param" ;
name        = IDENT { "." IDENT } | STRING ;                      (* STRING only for headers *)
type        = "string" | "int" | "number" | "bool"
            | "list" "<" type ">"
            | "enum" "(" STRING { "," STRING } ")" ;
constraint  = "matches" pat | "minlen" expr | "maxlen" expr
            | "in" expr ".." expr | "minitems" expr | "maxitems" expr ;
pat         = REGEX | IDENT ;

whenStmt    = "when" expr ( "require" | "forbid" ) name { "," name } ";" ;
blockStmt   = "block" "if" scope "matches" pat [ "as" STRING ] ";" ;
scope       = "any" "field" | "header" ( STRING | IDENT ) | "path" | "body" | name ;
bodyStmt    = "body" limit { limit } ";" ;
limit       = ( "max_depth" | "max_size" | "max_keys" ) expr ;

expr        = orExpr ;
orExpr      = andExpr { ( "||" | "or" ) andExpr } ;
andExpr     = notExpr { ( "&&" | "and" ) notExpr } ;
notExpr     = ( "!" | "not" ) notExpr | cmpExpr ;
cmpExpr     = addExpr [ ( "==" | "!=" | "<" | "<=" | ">" | ">=" ) addExpr ] ;
addExpr     = mulExpr { ( "+" | "-" ) mulExpr } ;
mulExpr     = unary { ( "*" | "/" ) unary } ;
unary       = "-" unary | primary ;
primary     = NUMBER [ UNIT ] | STRING | "true" | "false" | "exists" name | name | "(" expr ")" ;
```

## Regex sub-language (RegexShield v2)

```ebnf
regex       = alternation ;
alternation = concat { "|" concat } ;
concat      = { repeat } ;
repeat      = atom [ quant [ "?" ] ] ;
quant       = "*" | "+" | "?" | "{" n "}" | "{" n "," "}" | "{" n "," m "}" ;      (* n, m ≤ 100 *)
atom        = char | escape | class | "." | "(" regex ")" | "(?:" regex ")" | "^" | "$" ;
class       = "[" [ "^" ] item { item } "]" ;      item = char [ "-" char ] | escape ;
escape      = "\d" | "\D" | "\w" | "\W" | "\s" | "\S" | "\n" | "\t" | "\r" | "\xHH" | "\uHHHH" | "\" meta ;
```

Anchors may only appear at the start or end of the pattern, or of a top-level alternative. Back-references, look-around and `\b` are outside the supported subset and are rejected with a diagnostic. Matching uses search semantics. A missing `^` or `$` is modelled as an implicit `Σ*`. `$` accepts absolute end or the position before one final LF, following Python's default behavior. ASCII `\d`, `\w`, `\s` and ASCII case folding are used. This subset does not promise compatibility with every Python or JavaScript regex dialect detail.

## Semantics, briefly

* A request is routed by method and path (`{name}` segments become path parameters). If no rule matches, the request is denied.
* Each declared field is required unless it is marked `?`. Values in a header, query or path are converted to the declared scalar type.
* The checks for one field stop at that field's first violation. The other fields are still checked, so all violations are reported.
* `when C require x` reports an error if `C` holds and `x` is missing. `forbid` reports an error if `x` is present.
* `block if S matches P` rejects the request if any string in scope `S` matches `P`.
* Failed body limits stop validation before field checks and scans. Depth and key traversal are iterative and stop once the configured bound is exceeded.
* Header/query/path `number` coercion accepts `-?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?`. Whitespace, hex, binary, `+` prefixes and non-finite values are rejected. Integer coercion accepts decimal digits with an optional minus, within JavaScript's safe integer range.
* `max_size` measures compact UTF-8 JSON serialization of the parsed body, not the original HTTP bytes. Numeric formatting and extreme numeric values remain backend interoperability limits; integrations should enforce actual raw-body byte limits before parsing.
* Field verification covers scalar strings/enums, presence, length constraints and DFA pattern checks. It does not verify error identity/order, conditions, scans, numeric/list fields, complete requests, or generated backend code.
