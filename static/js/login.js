// Botão de mostrar/ocultar a senha na tela de login.
document.querySelectorAll(".mostrar-senha").forEach((botao) => {
  botao.addEventListener("click", () => {
    const campo = botao.parentElement.querySelector("input");
    campo.type = campo.type === "password" ? "text" : "password";
  });
});
