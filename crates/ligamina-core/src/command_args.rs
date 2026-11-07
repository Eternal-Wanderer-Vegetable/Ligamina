//! Structured command arguments (TOOL-03).
//!
//! Builds an MCP input schema from a command's declared arguments and options, and
//! turns structured input back into the command text the framework parses natively.
//! The raw `arguments` string stays available for every command.

use serde_json::{Map, Value, json};

use crate::commands::CommandDescriptor;

/// Field holding the raw text after the command name.
pub const RAW_FIELD: &str = "arguments";
/// Field holding declared options, keyed by option name.
pub const OPTIONS_FIELD: &str = "options";
const RAW_MAX_BYTES: u64 = 4096;

/// Input schema for a command tool. Only Koishi declares typed arguments today;
/// other frameworks (or declarations that would collide with the reserved fields)
/// get the raw text field alone.
pub fn input_schema(descriptor: &CommandDescriptor) -> Value {
    let raw = json!({
        "type": "string",
        "maxLength": RAW_MAX_BYTES,
        "description": "Text after the command name, parsed natively by the plugin framework; pass an empty string when the command takes no arguments. Use either this field or the structured fields, not both.",
    });
    let mut properties = Map::new();
    properties.insert(RAW_FIELD.to_owned(), raw);
    // Clients use `required` to decide whether a call needs arguments at all; with
    // nothing required, some bots call the tool directly with `{}` and lose the
    // user's input. Free-text commands therefore require the raw field (an empty
    // string means "no arguments"); typed commands require their declared arguments.
    let mut required: Vec<Value> = Vec::new();
    if supports_structured(descriptor) {
        for argument in arguments(descriptor) {
            let mut schema = json_type(argument.kind);
            if let Value::Object(object) = &mut schema {
                let mut text = format!(
                    "Positional argument{}",
                    if argument.required { " (required)" } else { "" }
                );
                if let Some(description) = &argument.description {
                    text.push_str(&format!(": {description}"));
                }
                object.insert("description".to_owned(), Value::String(text));
            }
            if argument.required {
                required.push(Value::String(argument.name.to_owned()));
            }
            properties.insert(argument.name.to_owned(), schema);
        }
        let mut option_properties = Map::new();
        for option in options(descriptor) {
            let mut schema = json_type(option.kind);
            if let Value::Object(object) = &mut schema
                && let Some(description) = &option.description
            {
                object.insert("description".to_owned(), Value::String(description.clone()));
            }
            option_properties.insert(option.name.to_owned(), schema);
        }
        if !option_properties.is_empty() {
            properties.insert(
                OPTIONS_FIELD.to_owned(),
                json!({
                    "type": "object",
                    "properties": option_properties,
                    "additionalProperties": false,
                    "description": "Command options by name.",
                }),
            );
        }
    } else {
        required.push(Value::String(RAW_FIELD.to_owned()));
    }
    let mut schema =
        json!({ "type": "object", "properties": properties, "additionalProperties": false });
    if !required.is_empty() {
        schema["required"] = Value::Array(required);
    }
    schema
}

/// Turns tool input into the text after the command name.
pub fn compose(
    descriptor: &CommandDescriptor,
    input: &Map<String, Value>,
) -> Result<String, String> {
    let structured = supports_structured(descriptor);
    let declared = arguments(descriptor);
    for key in input.keys() {
        let known = key == RAW_FIELD
            || (structured
                && (key == OPTIONS_FIELD || declared.iter().any(|argument| argument.name == key)));
        if !known {
            return Err(format!("unexpected argument field: {key}"));
        }
    }
    let structured_keys = input.keys().filter(|key| key.as_str() != RAW_FIELD).count();
    if let Some(raw) = input.get(RAW_FIELD) {
        if structured_keys > 0 {
            return Err(format!(
                "use either `{RAW_FIELD}` or the structured fields, not both"
            ));
        }
        return raw
            .as_str()
            .map(str::to_owned)
            .ok_or_else(|| format!("`{RAW_FIELD}` must be a string"));
    }
    if structured_keys == 0 {
        return Ok(String::new());
    }

    let mut parts = Vec::new();
    // Options go first: Koishi's trailing `text` argument consumes the rest of the line.
    if let Some(value) = input.get(OPTIONS_FIELD) {
        let Value::Object(values) = value else {
            return Err(format!("`{OPTIONS_FIELD}` must be an object"));
        };
        let declared_options = options(descriptor);
        if let Some(unknown) = values
            .keys()
            .find(|name| !declared_options.iter().any(|option| option.name == *name))
        {
            return Err(format!("unknown option: {unknown}"));
        }
        // Declaration order keeps the composed text deterministic.
        for option in &declared_options {
            let Some(value) = values.get(option.name) else {
                continue;
            };
            let name = option.name;
            match (option.kind, value) {
                ("boolean", Value::Bool(true)) => parts.push(option.flag.clone()),
                ("boolean", Value::Bool(false)) | (_, Value::Null) => {}
                ("boolean", _) => return Err(format!("option {name} must be a boolean")),
                (kind, value) => {
                    parts.push(option.flag.clone());
                    parts.push(quote(&scalar(name, kind, value)?, false, name)?);
                }
            }
        }
    }

    let mut missing_optional: Option<&str> = None;
    let provided = declared.iter().rposition(|argument| {
        input
            .get(argument.name)
            .is_some_and(|value| !value.is_null())
    });
    for (index, argument) in declared.iter().enumerate() {
        match input.get(argument.name).filter(|value| !value.is_null()) {
            None if argument.required => {
                return Err(format!("missing required argument: {}", argument.name));
            }
            None => missing_optional = missing_optional.or(Some(argument.name)),
            Some(value) => {
                if let Some(gap) = missing_optional {
                    return Err(format!(
                        "argument {} needs the earlier argument {gap}",
                        argument.name
                    ));
                }
                let text = scalar(argument.name, argument.kind, value)?;
                let rest = argument.kind == "text" && Some(index) == provided;
                parts.push(quote(&text, rest, argument.name)?);
            }
        }
    }
    Ok(parts.join(" "))
}

fn supports_structured(descriptor: &CommandDescriptor) -> bool {
    descriptor.framework == "koishi"
        && (!descriptor.arguments.is_empty() || !descriptor.options.is_empty())
        && arguments(descriptor)
            .iter()
            .all(|argument| argument.name != RAW_FIELD && argument.name != OPTIONS_FIELD)
}

struct Argument<'a> {
    name: &'a str,
    kind: &'a str,
    required: bool,
    description: Option<String>,
}

struct OptionSpec<'a> {
    name: &'a str,
    kind: &'a str,
    flag: String,
    description: Option<String>,
}

fn arguments(descriptor: &CommandDescriptor) -> Vec<Argument<'_>> {
    descriptor
        .arguments
        .iter()
        .filter_map(|value| {
            Some(Argument {
                name: value.get("name")?.as_str()?,
                kind: value
                    .get("type")
                    .and_then(Value::as_str)
                    .unwrap_or("string"),
                required: value
                    .get("required")
                    .and_then(Value::as_bool)
                    .unwrap_or(false),
                description: value
                    .get("description")
                    .and_then(Value::as_str)
                    .map(str::to_owned),
            })
        })
        .collect()
}

fn options(descriptor: &CommandDescriptor) -> Vec<OptionSpec<'_>> {
    descriptor
        .options
        .iter()
        .filter_map(|value| {
            let name = value.get("name")?.as_str()?;
            let syntax = value
                .get("syntax")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let flag = syntax
                .split([',', ' '])
                .find(|token| token.starts_with("--"))
                .map(str::to_owned)
                .unwrap_or_else(|| format!("--{name}"));
            // Koishi reports flags without a value as type boolean.
            let kind = value.get("type").and_then(Value::as_str).unwrap_or(
                if syntax.contains('<') || syntax.contains('[') {
                    "string"
                } else {
                    "boolean"
                },
            );
            Some(OptionSpec {
                name,
                kind,
                flag,
                description: value
                    .get("description")
                    .and_then(Value::as_str)
                    .map(str::to_owned),
            })
        })
        .collect()
}

fn json_type(kind: &str) -> Value {
    match kind {
        "number" => json!({ "type": "number" }),
        "integer" => json!({ "type": "integer" }),
        "posint" => json!({ "type": "integer", "minimum": 1 }),
        "natural" => json!({ "type": "integer", "minimum": 0 }),
        "boolean" => json!({ "type": "boolean" }),
        _ => json!({ "type": "string" }),
    }
}

fn scalar(name: &str, kind: &str, value: &Value) -> Result<String, String> {
    match (kind, value) {
        ("number" | "integer" | "posint" | "natural", Value::Number(number)) => {
            Ok(number.to_string())
        }
        ("number" | "integer" | "posint" | "natural", _) => Err(format!("{name} must be a number")),
        (_, Value::String(text)) => Ok(text.clone()),
        (_, Value::Number(number)) => Ok(number.to_string()),
        (_, Value::Bool(flag)) => Ok(flag.to_string()),
        _ => Err(format!("{name} must be a string, number or boolean")),
    }
}

/// Quotes a token for Koishi's argv parser, which has no escape for `"` inside quotes.
fn quote(text: &str, rest_of_line: bool, name: &str) -> Result<String, String> {
    if rest_of_line && !text.trim().is_empty() {
        return Ok(text.to_owned());
    }
    let needs_quotes = text.is_empty()
        || text.chars().any(char::is_whitespace)
        || text.starts_with('-')
        || text.contains('"');
    if !needs_quotes {
        return Ok(text.to_owned());
    }
    if text.contains('"') {
        return Err(format!(
            "{name} contains a double quote, which cannot be passed as a structured value; use the raw `{RAW_FIELD}` field"
        ));
    }
    Ok(format!("\"{text}\""))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn greet() -> CommandDescriptor {
        serde_json::from_value(json!({
            "command_id": "greet",
            "invocation": "greet",
            "framework": "koishi",
            "arguments": [
                { "name": "name", "required": true, "type": "string" },
                { "name": "times", "required": false, "type": "number" },
                { "name": "rest", "required": false, "type": "text" }
            ],
            "options": [
                { "name": "loud", "syntax": "-l, --loud", "type": "boolean" },
                { "name": "prefix", "syntax": "-p, --prefix <prefix>", "type": "string" },
                { "name": "count", "syntax": "-c, --count <count>", "type": "number" }
            ]
        }))
        .unwrap()
    }

    fn input(value: Value) -> Map<String, Value> {
        let Value::Object(map) = value else { panic!() };
        map
    }

    #[test]
    fn schema_lists_typed_arguments_and_options_next_to_raw_text() {
        let schema = input_schema(&greet());
        let properties = &schema["properties"];
        assert_eq!(properties[RAW_FIELD]["type"], "string");
        assert_eq!(properties["name"]["type"], "string");
        assert_eq!(properties["times"]["type"], "number");
        assert_eq!(
            properties[OPTIONS_FIELD]["properties"]["loud"]["type"],
            "boolean"
        );
        assert_eq!(
            properties[OPTIONS_FIELD]["properties"]["count"]["type"],
            "number"
        );
        assert_eq!(schema["additionalProperties"], false);
        assert_eq!(schema["required"], json!(["name"]));
    }

    #[test]
    fn composes_options_first_and_quotes_only_when_needed() {
        let text = compose(
            &greet(),
            &input(json!({
                "name": "Ada Lovelace", "times": 3, "rest": "hello -l world",
                "options": { "loud": true, "prefix": "Dr. X", "count": 2 }
            })),
        )
        .unwrap();
        assert_eq!(
            text,
            "--loud --prefix \"Dr. X\" --count 2 \"Ada Lovelace\" 3 hello -l world"
        );
        assert_eq!(
            compose(&greet(), &input(json!({ "name": "bob" }))).unwrap(),
            "bob"
        );
        assert_eq!(
            compose(
                &greet(),
                &input(json!({ "name": "-x", "options": { "loud": false } }))
            )
            .unwrap(),
            "\"-x\""
        );
        assert_eq!(compose(&greet(), &input(json!({}))).unwrap(), "");
        assert_eq!(
            compose(&greet(), &input(json!({ RAW_FIELD: "anything goes" }))).unwrap(),
            "anything goes"
        );
    }

    #[test]
    fn rejects_ambiguous_or_invalid_structured_input() {
        let cases = [
            json!({ RAW_FIELD: "x", "name": "y" }),
            json!({ "times": 3 }),
            json!({ "name": "a", "rest": "b" }),
            json!({ "name": "a\"b" }),
            json!({ "name": "a", "times": "three" }),
            json!({ "name": "a", "options": { "unknown": 1 } }),
            json!({ "name": "a", "options": { "loud": "yes" } }),
            json!({ "name": "a", "extra": 1 }),
        ];
        for case in cases {
            assert!(compose(&greet(), &input(case.clone())).is_err(), "{case}");
        }
    }

    #[test]
    fn frameworks_without_declarations_keep_raw_text_only() {
        let nonebot: CommandDescriptor = serde_json::from_value(json!({
            "command_id": "roll", "invocation": "/roll", "framework": "nonebot"
        }))
        .unwrap();
        let schema = input_schema(&nonebot);
        assert_eq!(schema["properties"].as_object().unwrap().len(), 1);
        // Free-text commands must say input is required, or clients may call them with `{}`.
        assert_eq!(schema["required"], json!([RAW_FIELD]));
        assert!(compose(&nonebot, &input(json!({ "dice": "1d6" }))).is_err());
        assert_eq!(
            compose(&nonebot, &input(json!({ RAW_FIELD: "1d6" }))).unwrap(),
            "1d6"
        );
    }
}
