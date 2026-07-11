def call() {
    echo 'EXAMPLE_LIBRARY_EXECUTED'
    writeFile file: 'shared-library-proof.txt', text: 'example library ran\n'
    archiveArtifacts artifacts: 'shared-library-proof.txt', fingerprint: false
}
