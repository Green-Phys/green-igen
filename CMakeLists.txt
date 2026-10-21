cmake_minimum_required(VERSION 3.18)
set(CMAKE_DISABLE_SOURCE_CHANGES ON)
set(CMAKE_DISABLE_IN_SOURCE_BUILD ON)
project(igen C CXX)

if (${CMAKE_CURRENT_SOURCE_DIR} STREQUAL ${CMAKE_CURRENT_BINARY_DIR})
    message(FATAL_ERROR "In-source builds are not allowed")
endif ()

################ GREEN RELEASE VERSION #####################
set(GREEN_RELEASE origin/main)

set(CMAKE_CXX_STANDARD 17)
# The C sources (and the libcint v4.4.6 fetched below) predate C23, which GCC 15 uses by default: there, an empty parameter
# list "()" means "no arguments", so libcint's function-pointer tables fail with -Wincompatible-pointer-types errors.
# Build all C code as C17 with GNU extensions (gnu17), the default of GCC 8-14. Set before FetchContent so libcint inherits it.
set(CMAKE_C_STANDARD 17)
set(CMAKE_C_EXTENSIONS ON)

find_package(BLAS REQUIRED)
find_package(OpenMP)
if(OPENMP_FOUND)
  set(HAVE_OPENMP 1)
else ()
  set(OpenMP_C_FLAGS " ")
endif()


if (WIN32)
  #?
elseif (APPLE)
  #TODO: test me set(CMAKE_MACOSX_RPATH On)
else ()
  set(CMAKE_SKIP_BUILD_RPATH  True)
  set(CMAKE_BUILD_WITH_INSTALL_RPATH True)
  set(CMAKE_INSTALL_RPATH "\$ORIGIN:\$ORIGIN/green_igen")
  set(INSTALL_RPATH_USE_LINK_PATH 1)
endif ()

if (NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE RELWITHDEBINFO)
endif()
#set(CMAKE_BUILD_TYPE DEBUG)
set(CMAKE_VERBOSE_MAKEFILE OFF)
if (CMAKE_COMPILER_IS_GNUCC) # Does it skip the link flag on old OsX?
  # TODO: If updating to minimum requirement cmake>=3.7, use
  # CMAKE_SHARED_LINKER_FLAGS_INIT to combine LDFLAGS options.
  # https://cmake.org/cmake/help/v3.7/variable/CMAKE_SHARED_LINKER_FLAGS_INIT.html
  # see also issue #661
  if(UNIX AND NOT APPLE AND NOT DEFINED ENV{LDFLAGS})
    set(CMAKE_SHARED_LINKER_FLAGS "-Wl,--no-as-needed")
  endif()
endif()
set(CMAKE_C_FLAGS "-Wall ${CMAKE_C_FLAGS}")

option(BUILD_MARCH_NATIVE "gcc flag -march=native" off)
if (BUILD_MARCH_NATIVE)
  include(CheckCCompilerFlag)
  CHECK_C_COMPILER_FLAG("-march=native" COMPILER_SUPPORTS_MARCH_NATIVE)
  if(COMPILER_SUPPORTS_MARCH_NATIVE)
    if ("${CMAKE_C_COMPILER_ID}" STREQUAL "Intel")
      message("Add CFLAGS -march=native -unroll-aggressive")
      set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -unroll-aggressive -ipo")
      set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -fno-math-errno")
    else()
      message("Add CFLAGS -march=native -ftree-vectorize")
      set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -march=native -funroll-loops -ftree-vectorize")
      set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -fno-math-errno")
    endif()
  endif()
endif()

set(C_LINK_TEMPLATE "<CMAKE_C_COMPILER> <CMAKE_SHARED_LIBRARY_C_FLAGS> <LANGUAGE_COMPILE_FLAGS> <LINK_FLAGS> <CMAKE_SHARED_LIBRARY_CREATE_C_FLAGS> -o <TARGET> <OBJECTS> <LINK_LIBRARIES>")
set(CXX_LINK_TEMPLATE "<CMAKE_CXX_COMPILER> <CMAKE_SHARED_LIBRARY_CXX_FLAGS> <LANGUAGE_COMPILE_FLAGS> <LINK_FLAGS> <CMAKE_SHARED_LIBRARY_CREATE_CXX_FLAGS> -o <TARGET> <OBJECTS> <LINK_LIBRARIES>")

include(ExternalProject)
option(BUILD_LIBCINT "Using libcint for analytical gaussian integral" ON)
option(WITH_F12 "Compling F12 integrals" ON)


Include(FetchContent)
FetchContent_Declare(
        libcint
        GIT_REPOSITORY https://github.com/sunqm/libcint.git
        GIT_TAG v4.4.6 # or a later release
)
set(libcint_WITH_CINT2_INTERFACE ${WITH_CINT2_INTERFACE})
set(libcint_WITH_RANGE_COULOMB ${WITH_RANGE_COULOMB})
set(libcint_WITH_FORTRAN ${WITH_FORTRAN})
set(libcint_MIN_EXPCUTOFF ${MIN_EXPCUTOFF})
set(libcint_KEEP_GOING ${KEEP_GOING})
set(libcint_CMAKE_C_CREATE_SHARED_LIBRARY "${CMAKE_C_CREATE_SHARED_LIBRARY}")

option(WITH_CINT2_INTERFACE "Enable old interface" OFF)
set(WITH_RANGE_COULOMB ON)
option(WITH_FORTRAN "Enable old interface" 0FF)
set(MIN_EXPCUTOFF 20)
set(KEEP_GOING 1)
set(CMAKE_C_CREATE_SHARED_LIBRARY "${C_LINK_TEMPLATE}")
FetchContent_GetProperties(libcint)
if(NOT libcint_POPULATED)
    FetchContent_Populate(libcint)
    add_subdirectory(${libcint_SOURCE_DIR} ${libcint_BINARY_DIR} EXCLUDE_FROM_ALL)
    set_property(TARGET cint PROPERTY SOVERSION)
    set_property(TARGET cint PROPERTY VERSION)
endif()

set(WITH_CINT2_INTERFACE ${libcint_WITH_CINT2_INTERFACE})
set(WITH_RANGE_COULOMB ${libcint_WITH_RANGE_COULOMB})
set(WITH_FORTRAN ${libcint_WITH_FORTRAN})
set(MIN_EXPCUTOFF ${libcint_MIN_EXPCUTOFF})
set(KEEP_GOING ${libcint_KEEP_GOING})
set(CMAKE_C_CREATE_SHARED_LIBRARY ${libcint_CMAKE_C_CREATE_SHARED_LIBRARY})

check_library_exists(quadmath logq "" HAVE_LIBQUADMATH)
check_include_file("quadmath.h" HAVE_QUADMATH_H)

add_subdirectory(src)
add_library(GREEN::IGEN ALIAS pbc0)
add_dependencies(pbc0 cint)

target_include_directories(pbc0 PUBLIC "${cint_BINARY_DIR}/include")
target_include_directories(pbc0 PUBLIC "${cint_SOURCE_DIR}/include")

message(STATUS ${CMAKE_INSTALL_PREFIX})
install(TARGETS pbc0 DESTINATION green_igen)

