#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>

const char* ssid     = "Super Gaming";
const char* password = "88888888";

#define LED_TUOI   2    
#define LED_DEN    4    
#define LED_QUAT   5    
#define LED_CUA   18    

WebServer server(80);

bool ledState[4] = {false, false, false, false};
String lastCmd = "San sang";

void applyLEDs() {
  digitalWrite(LED_TUOI, ledState[0] ? HIGH : LOW);
  digitalWrite(LED_DEN,  ledState[1] ? HIGH : LOW);
  digitalWrite(LED_QUAT, ledState[2] ? HIGH : LOW);
  digitalWrite(LED_CUA,  ledState[3] ? HIGH : LOW);
}

String getStatusJson() {
  return "{\"tuoi\":"  + String(ledState[0] ? "true" : "false") +
         ",\"den\":"   + String(ledState[1] ? "true" : "false") +
         ",\"quat\":"  + String(ledState[2] ? "true" : "false") +
         ",\"cua\":"   + String(ledState[3] ? "true" : "false") +
         ",\"last_cmd\":\"" + lastCmd + "\"}";
}

String processCommand(String cmd) {
  cmd.toLowerCase();
  cmd.trim();

  if (cmd.indexOf("bat tuoi") >= 0 || cmd.indexOf("tuoi nuoc") >= 0) {
    ledState[0] = true;  applyLEDs(); lastCmd = "Bat tuoi";
    return "{\"status\":\"ok\",\"msg\":\"Da bat tuoi nuoc\"}";
  }
  if (cmd.indexOf("tat tuoi") >= 0) {
    ledState[0] = false; applyLEDs(); lastCmd = "Tat tuoi";
    return "{\"status\":\"ok\",\"msg\":\"Da tat tuoi nuoc\"}";
  }

  if (cmd.indexOf("bat den") >= 0) {
    ledState[1] = true;  applyLEDs(); lastCmd = "Bat den";
    return "{\"status\":\"ok\",\"msg\":\"Da bat den\"}";
  }
  if (cmd.indexOf("tat den") >= 0) {
    ledState[1] = false; applyLEDs(); lastCmd = "Tat den";
    return "{\"status\":\"ok\",\"msg\":\"Da tat den\"}";
  }

  if (cmd.indexOf("bat quat") >= 0) {
    ledState[2] = true;  applyLEDs(); lastCmd = "Bat quat";
    return "{\"status\":\"ok\",\"msg\":\"Da bat quat\"}";
  }
  if (cmd.indexOf("tat quat") >= 0) {
    ledState[2] = false; applyLEDs(); lastCmd = "Tat quat";
    return "{\"status\":\"ok\",\"msg\":\"Da tat quat\"}";
  }

  if (cmd.indexOf("mo cua") >= 0) {
    ledState[3] = true;  applyLEDs(); lastCmd = "Mo cua";
    return "{\"status\":\"ok\",\"msg\":\"Da mo cua\"}";
  }
  if (cmd.indexOf("dong cua") >= 0) {
    ledState[3] = false; applyLEDs(); lastCmd = "Dong cua";
    return "{\"status\":\"ok\",\"msg\":\"Da dong cua\"}";
  }

  if (cmd.indexOf("bat tat ca") >= 0) {
    for (int i = 0; i < 4; i++) ledState[i] = true;
    applyLEDs(); lastCmd = "Bat tat ca";
    return "{\"status\":\"ok\",\"msg\":\"Da bat tat ca\"}";
  }
  if (cmd.indexOf("tat tat ca") >= 0) {
    for (int i = 0; i < 4; i++) ledState[i] = false;
    applyLEDs(); lastCmd = "Tat tat ca";
    return "{\"status\":\"ok\",\"msg\":\"Da tat tat ca\"}";
  }

  if (cmd.indexOf("trang thai") >= 0) {
    lastCmd = "Trang thai";
    String msg = "Tuoi:" + String(ledState[0] ? "ON" : "OFF") +
                 " Den:"  + String(ledState[1] ? "ON" : "OFF") +
                 " Quat:" + String(ledState[2] ? "ON" : "OFF") +
                 " Cua:"  + String(ledState[3] ? "ON" : "OFF");
    return "{\"status\":\"ok\",\"msg\":\"" + msg + "\"}";
  }

  lastCmd = "Lenh la?";
  return "{\"status\":\"error\",\"msg\":\"Khong hieu: " + cmd + "\"}";
}

void handleCommand() {
  String cmd = server.hasArg("cmd") ? server.arg("cmd") : "";
  Serial.println("Lenh: " + cmd);
  server.send(200, "application/json", processCommand(cmd));
}

void handleStatus() {
  server.send(200, "application/json", getStatusJson());
}

void setup() {
  Serial.begin(115200);

  pinMode(LED_TUOI, OUTPUT);
  pinMode(LED_DEN,  OUTPUT);
  pinMode(LED_QUAT, OUTPUT);
  pinMode(LED_CUA,  OUTPUT);
  applyLEDs();

  Serial.print("Dang ket WiFi");
  WiFi.begin(ssid, password);
  int tries = 0;
  while (WiFi.status() != WL_CONNECTED && tries < 20) {
    delay(500); Serial.print("."); tries++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\nESP32 IP: " + WiFi.localIP().toString());
  } else {
    Serial.println("\nWiFi that bai! Kiem tra ssid/password.");
  }

  server.on("/", HTTP_GET, []() {
    server.send(200, "text/plain",
      "ESP32 Trang Trai\n"
      "GET /cmd?cmd=bat tuoi\n"
      "GET /cmd?cmd=tat tuoi\n"
      "GET /cmd?cmd=bat den\n"
      "GET /cmd?cmd=tat den\n"
      "GET /cmd?cmd=bat quat\n"
      "GET /cmd?cmd=tat quat\n"
      "GET /cmd?cmd=mo cua\n"
      "GET /cmd?cmd=dong cua\n"
      "GET /cmd?cmd=bat tat ca\n"
      "GET /cmd?cmd=tat tat ca\n"
      "GET /status");
  });
  server.on("/cmd",    HTTP_GET,  handleCommand);
  server.on("/cmd",    HTTP_POST, handleCommand);
  server.on("/status", HTTP_GET,  handleStatus);
  server.begin();
  Serial.println("Server da chay!");
}

void loop() {
  server.handleClient();
}
