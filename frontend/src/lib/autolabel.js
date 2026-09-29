export const sourceLabel = (source) =>
  source === "model" ? "trained model"
  : source === "gemini" ? "Gemini"
  : source === "model+gemini" ? "model + Gemini"
  : source === "model_no_fallback" ? "model (fallback off)"
  : source;
