document.addEventListener("DOMContentLoaded", () => {
  const modal = document.getElementById("modal");
  const modalTitle = document.getElementById("modalTitle");
  const closeBtn = document.querySelector(".close-btn");
  const navDemoBtn = document.getElementById("navDemoBtn");
  const heroDemoBtn = document.getElementById("heroDemoBtn");
  const contactBtn = document.getElementById("contactBtn");
  const modalForm = document.getElementById("modalForm");

  // Open modal with specific title
  function openModal(title) {
    modalTitle.textContent = title;
    modal.style.display = "flex";
  }

  // Close modal
  function closeModal() {
    modal.style.display = "none";
  }

  // Event Listeners
  navDemoBtn.addEventListener("click", () => openModal("Request A Demo"));
  heroDemoBtn.addEventListener("click", () => openModal("Request A Demo"));
  contactBtn.addEventListener("click", () => openModal("Contact Us"));

  closeBtn.addEventListener("click", closeModal);

  window.addEventListener("click", (e) => {
    if (e.target === modal) closeModal();
  });

  // Handle Form Submission
  modalForm.addEventListener("submit", (e) => {
    e.preventDefault();
    alert("Thank you! We have received your request.");
    closeModal();
    modalForm.reset();
  });
});