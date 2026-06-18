// Hamburger menu toggle
const navToggle = document.getElementById("nav-toggle");
const navMobileMenu = document.getElementById("nav-mobile-menu");
if (navToggle && navMobileMenu) {
  navToggle.addEventListener("click", () => {
    navMobileMenu.classList.toggle("open");
  });
  // Close menu when a link is clicked
  navMobileMenu.querySelectorAll("a").forEach((link) => {
    link.addEventListener("click", () => navMobileMenu.classList.remove("open"));
  });
}