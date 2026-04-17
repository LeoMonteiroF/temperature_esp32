# **ADR 001: Controle de Atuadores Tuya via Proxy Local (ESP32 Polling)**

## **Status**

Aceito

## **Contexto**

O projeto requer o acionamento remoto de uma tomada inteligente Positivo (ecossistema Tuya) a partir de um servidor na nuvem (Render) para controlar um elemento de aquecimento/arrefecimento. O método oficial para desenvolvedores (Tuya IoT Platform) apresenta alta instabilidade e risco de indisponibilidade de serviço. Soluções de terceiros (IFTTT, Voice Monkey) limitaram os seus planos gratuitos ou removeram suporte a gatilhos. É necessário um mecanismo robusto, de baixo custo e imune a mudanças de políticas de plataformas intermediárias.

## **Decisão**

A arquitetura utilizará o padrão de **HTTP Polling** a rodar numa **ESP32** na rede local, atuando como um "Proxy de Estado" entre o servidor Render e o ecossistema do Google Home.

### **Fluxo de Execução**

1. **Servidor (Render):** Mantém o estado desejado da tomada num endpoint HTTP GET /check-status.  
2. **Proxy Local (ESP32):** Realiza polling periódico (ex: a cada 30 a 60 segundos) neste endpoint.  
3. **Emulação Local:** A ESP32 utiliza a biblioteca Espalexa (ou similar) para simular um dispositivo inteligente na rede local. Ao identificar o comando "on" do Render, a ESP32 altera o seu próprio estado virtual.  
4. **Acionamento Final:** O *Google Home Script Editor* monitora o estado da ESP32. Ao detetar a mudança, aciona a tomada Positivo através da integração oficial (Google \-\> Tuya).

### **Implementação de Referência**

**Servidor (Node.js/Render):**

let tomadaStatus \= "off";  
let ultimaLeituraTimestamp \= Date.now();

app.post('/sensor-update', (req, res) \=\> {  
    // Atualiza a temperatura e renova o timestamp  
    ultimaLeituraTimestamp \= Date.now();  
    // Lógica de termostato define o tomadaStatus aqui...  
    res.send("Sensor atualizado");  
});

app.get('/check-status', (req, res) \=\> {  
    // Calcula há quantos segundos a última medição foi feita  
    const idadeLeituraSegundos \= Math.floor((Date.now() \- ultimaLeituraTimestamp) / 1000);  
      
    res.json({   
        status: tomadaStatus,  
        idade\_leitura\_segundos: idadeLeituraSegundos  
    });  
});

**Proxy (ESP32 \- C++):**

// Tempo máximo aceitável sem novas leituras do sensor (ex: 5 minutos)  
const int MAX\_IDADE\_LEITURA\_SEC \= 300; 

void loop() {  
  if (WiFi.status() \== WL\_CONNECTED) {  
    HTTPClient http;  
    http.begin("\[https://seu-app.render.com/check-status\](https://seu-app.render.com/check-status)");  
    int httpCode \= http.GET();

    if (httpCode \> 0\) {  
      // Usar ArduinoJson para extrair os dados com segurança  
      DynamicJsonDocument doc(1024);  
      deserializeJson(doc, http.getString());  
        
      String status \= doc\["status"\];  
      int idadeLeitura \= doc\["idade\_leitura\_segundos"\];

      // Failsafe (Dead Man's Switch): Se a leitura for muito velha, força desligamento  
      if (idadeLeitura \> MAX\_IDADE\_LEITURA\_SEC) {  
          dispositivoVirtual.setValue(0); // OFF de segurança  
      } else {  
          // Operação normal baseada no comando do servidor  
          if (status \== "on") {  
              dispositivoVirtual.setValue(255);  
          } else {  
              dispositivoVirtual.setValue(0);  
          }  
      }  
    } else {  
        // Failsafe de rede: Render fora do ar ou sem Wi-Fi  
        dispositivoVirtual.setValue(0);   
    }  
    http.end();  
  }  
  delay(30000); // Consulta a cada 30 segundos  
}

## **Defesa Técnica (Justificativas e Mitigações)**

* **Autonomia contra Ingerência de Terceiros:** Removemos o Portal de Desenvolvedores da Tuya e os Brokers MQTT da equação. A arquitetura depende unicamente do servidor proprietário (Render) e da integração Google Home-Tuya (focada no utilizador final, cujo uptime é historicamente superior às APIs de dev).  
* **Failsafe Térmico (Dead Man's Switch):** A introdução do parâmetro idade\_leitura\_segundos protege o sistema contra falhas silenciosas. Se o sensor físico parar de transmitir ou o servidor congelar, o payload indicará dados obsoletos. A ESP32 atuará cortando o aquecimento de forma autônoma se os dados passarem do limite de confiança (ex: 5 minutos).  
* **Limitação de Estado Físico (Loop Aberto e Tolerância a Perturbações):** A ESP32 opera como um proxy cego em relação ao estado elétrico real da tomada Positivo. O feedback de malha fechada é feito no servidor Render analisando a curva de temperatura. Para evitar falsos positivos causados por perturbações externas (ex: abertura da porta), a lógica do servidor incorpora um mecanismo de **Retry**. Se a curva de temperatura contrariar o comando após o tempo de inércia inicial, o servidor deve reafirmar o estado e aguardar uma nova janela (ex: 5 minutos). Se a anomalia persistir, assume-se falha sistémica (relé colado, tomada off-line ou porta deixada aberta) e o Render deve disparar um alerta crítico via webhook (ex: notificação Telegram), isolando a falha sem exigir inteligência complexa na ESP32.