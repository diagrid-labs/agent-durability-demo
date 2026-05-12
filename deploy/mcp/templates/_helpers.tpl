{{- define "mcp.name" -}}mcp{{- end -}}

{{- define "mcp.labels" -}}
app.kubernetes.io/name: {{ include "mcp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: mcp-server
app.kubernetes.io/part-of: bank-heist
{{- end -}}

{{- define "mcp.selectorLabels" -}}
app.kubernetes.io/name: {{ include "mcp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
